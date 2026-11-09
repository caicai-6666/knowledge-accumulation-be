"""节点执行检查点：复用树事务和连接池，原子保存消息、生成及恢复快照。"""

from app.model.node_persistence import NodeGeneration, NodeMessage, RestoredNode
from pydantic import TypeAdapter
from app.model.question_file import AttachmentReference,FileID
from app.model.question_tree import NodeWrite, QuestionNode, SummaryContent, TaskContent, WorkspaceVersion, new_tree_id, utc_now
from app.repository.question_tree import MySQLQuestionTreeRepository, QuestionTreeConflict, _CONTENT, _date, _decode, _dump, _row


class QuestionFileNotFound(LookupError):
    """附件没有登记在指定树中，不泄露其他树的文件归属。"""


class MySQLNodeRepository:
    def __init__(self, trees: MySQLQuestionTreeRepository, *, commit_hook=None):
        # 聚合内复用同一事务提供者，不创建第二个连接池，也不负责关闭外部仓库。
        self.trees=trees
        # 可选数据库提交钩子，只允许同事务 SQL；不得在钩子内进行远程交付。
        self.commit_hook=commit_hook

    async def _node(self,cursor,tree_id,node_id):
        await cursor.execute("SELECT * FROM question_nodes WHERE tree_id=%s AND id=%s AND kind='task'",(tree_id,node_id))
        row=await cursor.fetchone()
        if row is None:
            raise ValueError('本树任务节点不存在')
        return _row(QuestionNode,row)

    async def _content(self,cursor,node_id):
        await cursor.execute('SELECT content FROM question_node_contents WHERE node_id=%s',(node_id,))
        row=await cursor.fetchone()
        if row is None:
            raise ValueError('任务正文缺失')
        content=_CONTENT.validate_python(_decode(row['content']))
        if not isinstance(content,TaskContent):
            raise ValueError('任务正文类型不合法')
        return content

    @staticmethod
    def _generation(row):
        values=dict(row)
        values['payload']=_decode(values['payload'])
        return _row(NodeGeneration,values)

    async def _restore(self,cursor,tree,node_id):
        node=await self._node(cursor,tree.id,node_id)
        content=await self._content(cursor,node_id)
        if tree.kind == 'assistant':
            from app.repository.assistant_memory import current_workspace
            workspace=await current_workspace(cursor,tree)
        else:
            workspace=await self.trees._workspace(cursor,tree.id,node.workspace_version_id)
        if workspace is None:
            raise ValueError('该节点缺少执行恢复快照，需显式迁移旧记录')
        await cursor.execute('SELECT * FROM question_node_messages WHERE node_id=%s ORDER BY sequence',(node_id,))
        messages=tuple(_row(NodeMessage,row) for row in await cursor.fetchall())
        await cursor.execute('SELECT * FROM question_node_generations WHERE node_id=%s ORDER BY sequence',(node_id,))
        generations=tuple(self._generation(row) for row in await cursor.fetchall())
        questions=[message for message in messages if message.kind=='question']
        finals=[message for message in messages if message.kind=='final' and message.status=='completed']
        if (len(questions)!=1 or questions[0].id!=node_id or questions[0].sequence!=1
                or questions[0].text!=content.question or len(finals)>1
                or bool(finals)!=(node.status=='complete')
                or content.answer!=(finals[0].text if finals else '')):
            raise ValueError('节点公开消息与正文或终态不一致')
        sequences=[event.sequence for event in (*messages,*generations)]
        if len(set(sequences))!=len(sequences) or sorted(sequences)!=list(range(1,len(sequences)+1)):
            raise ValueError('节点执行记录序号重复或缺失')
        return RestoredNode(tree,node,content,workspace,messages,generations)

    async def load_node(self,tree_id,owner_userid,node_id) -> RestoredNode:
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid)
            return await self._restore(cursor,tree,node_id)

    async def start_task(self,tree_id,owner_userid,*,node_id,question,parent_id=None,
                         expected_revision:int,advance_main:bool | None=None,
                         summary:SummaryContent | None=None,summary_id=None,attachments=(),
                         summary_workspace:WorkspaceVersion | None=None) -> RestoredNode:
        """调用方先生成稳定 node_id；首次问题、节点及快照在同一个事务落盘。"""
        content=TaskContent(question=question,attachments=attachments)
        if summary is not None:
            summary=SummaryContent.model_validate(summary.model_dump())
            if not summary_id or parent_id is None or summary.covered_to_id!=parent_id:
                raise ValueError('任务前摘要必须绑定所选父节点和稳定摘要标识')
        elif summary_id is not None:
            raise ValueError('摘要标识必须与摘要内容同时提供')
        if summary_workspace is not None:
            summary_workspace=WorkspaceVersion.model_validate(summary_workspace.model_dump())
            if summary is None or summary_workspace.tree_id!=tree_id:
                raise ValueError('摘要工作区必须绑定本树的已验收摘要')
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid,lock=True)
            if summary_workspace is not None and tree.kind!='assistant':
                raise ValueError('该入口仅用于助理唯一工作区')
            await cursor.execute('SELECT id FROM question_nodes WHERE tree_id=%s AND id=%s',(tree_id,node_id))
            if await cursor.fetchone() is not None:
                restored=await self._restore(cursor,tree,node_id)
                if (restored.content.question!=question or restored.content.attachments!=content.attachments
                        or restored.node.parent_id!=(summary_id if summary else parent_id)):
                    raise QuestionTreeConflict('重复节点标识对应不同输入或父节点')
                if summary is not None:
                    await cursor.execute('SELECT content FROM question_node_contents WHERE node_id=%s',(summary_id,))
                    row=await cursor.fetchone()
                    if row is None or _CONTENT.validate_python(_decode(row['content']))!=summary:
                        raise QuestionTreeConflict('重复摘要标识对应不同内容')
                return restored
            self.trees._revision(tree,expected_revision)
            return await self._start_locked(cursor,tree,node_id,content,parent_id,advance_main,summary,summary_id,summary_workspace)

    async def _start_locked(self,cursor,tree,node_id,content,parent_id,advance_main=None,summary=None,summary_id=None,summary_workspace=None):
        """调用方持树行锁；普通创建与用户替代复用同一份原子登记逻辑。"""
        tree_id=tree.id
        if tree.kind=='assistant':
            from app.repository.assistant_context import append_assistant_task
            from app.repository.assistant_memory import load_memory, commit_memory
            before=await load_memory(self.trees,cursor,tree)
            updated=await append_assistant_task(cursor,self.trees,tree,node_id,content,parent_id,
                                                advance_main,summary,summary_id)
            if summary_workspace is not None:
                await cursor.execute('UPDATE assistant_workspaces SET id=%s,content=%s,created_at=%s,updated_at=%s WHERE tree_id=%s',
                    (summary_workspace.id,_dump(summary_workspace.content),_date(summary_workspace.created_at),_date(utc_now()),tree_id))
            await self._insert_event(cursor,NodeMessage(id=node_id,node_id=node_id,kind='question',text=content.question,sequence=1))
            restored=await self._restore(cursor,updated,node_id)
            await commit_memory(self.trees,cursor,before,restored,summary=summary)
            return restored
        snapshot=await self.trees._snapshot(cursor,tree)
        parent=snapshot.nodes_by_id.get(parent_id)
        if parent_id is not None and parent is None:
            raise ValueError('父节点不属于本树')
        version_id=parent.workspace_version_id if parent else None
        if parent is not None and version_id is None:
            raise ValueError('父节点缺少恢复快照，需显式迁移旧记录')
        workspace=None
        if version_id is None:
            workspace=WorkspaceVersion(tree_id=tree_id,content={})
            version_id=workspace.id
        sequence=snapshot.nodes[-1].sequence+1 if snapshot.nodes else 1
        writes=[]
        if summary is not None:
            writes.append(NodeWrite(node=QuestionNode(id=summary_id,tree_id=tree_id,parent_id=parent_id,kind='summary',
                sequence=sequence,workspace_version_id=version_id),content=summary))
        node=QuestionNode(id=node_id,tree_id=tree_id,parent_id=summary_id if summary else parent_id,kind='task',status='generating',
            sequence=sequence+(1 if summary else 0),workspace_version_id=version_id)
        writes.append(NodeWrite(node=node,content=content))
        continues_main=parent_id==tree.main_leaf_id if advance_main is None else advance_main
        candidate=await self.trees._append_path_locked(cursor,tree,writes,
            advance_main=continues_main,workspaces=(workspace,) if workspace else ())
        message=NodeMessage(id=node_id,node_id=node_id,kind='question',text=content.question,sequence=1)
        await self._insert_event(cursor,message)
        return await self._restore(cursor,candidate.tree,node_id)

    async def cancel_task(self,tree_id,owner_userid,node_id,*,events=()):
        """控制操作在树锁内读取最新版本；重复停止幂等，不能改写其他终态。"""
        return await self._commit(tree_id,owner_userid,node_id,expected_revision=None,
            events=events,workspace=None,exchanges=None,terminal='cancelled',latest=True)

    async def fail_task(self,tree_id,owner_userid,node_id):
        """系统停机等控制故障使用 failed，不伪装成用户手动停止。"""
        return await self._commit(tree_id,owner_userid,node_id,expected_revision=None,
            events=(),workspace=None,exchanges=None,terminal='failed',latest=True)

    async def replace_task(self,tree_id,owner_userid,node_id,*,replacement_id,question,attachments=()):
        """原子结束旧执行、登记新问题和关联；任一步失败都不停止旧任务。"""
        content=TaskContent(question=question,attachments=attachments)
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid,lock=True)
            current=await self._restore(cursor,tree,node_id)
            if current.node.status=='superseded' and current.node.superseded_by_node_id==replacement_id:
                existing=await self._restore(cursor,tree,replacement_id)
                if existing.content.question!=content.question or existing.content.attachments!=content.attachments:
                    raise QuestionTreeConflict('接替节点标识对应不同用户输入')
                return existing
            if current.node.status!='generating' or replacement_id==node_id:
                raise QuestionTreeConflict('只能替代正在执行的任务；已结束任务应创建普通新节点')
            snapshot = await self.trees._snapshot(cursor, tree) if tree.kind == 'exploration' else None
            # 事务内临时关闭父节点，使共用构树校验允许追加；该中间状态不会对外可见。
            # 先插入新节点，再设置真实替代关系，满足同树外键与终态关联约束。
            finished_at = utc_now()
            await cursor.execute("UPDATE question_nodes SET status='cancelled',finished_at=%s WHERE id=%s",
                (_date(finished_at),node_id))
            if snapshot is not None:
                self.trees._update_snapshot(cursor, snapshot, tree,
                    current.node.model_copy(update={'status':'cancelled', 'finished_at':finished_at}))
            new=await self._start_locked(cursor,tree,replacement_id,content,node_id)
            await cursor.execute("UPDATE question_nodes SET status='superseded',superseded_by_node_id=%s WHERE id=%s",
                (replacement_id,node_id))
            if snapshot is not None:
                appended = await self.trees._snapshot(cursor, new.tree)
                self.trees._update_snapshot(cursor, appended, new.tree, current.node.model_copy(update={
                    'status':'superseded', 'finished_at':finished_at, 'superseded_by_node_id':replacement_id}))
            return await self._restore(cursor,new.tree,replacement_id)

    async def append_terminal_generation(self,tree_id,owner_userid,node_id,generation):
        """终态只允许追加私有原始生成，不接受迟到的工具动作、消息或工作区。"""
        generation=NodeGeneration.model_validate(generation.model_dump())
        if generation.node_id!=node_id or generation.sequence is not None:
            raise ValueError('私有生成必须绑定本节点且由仓库分配序号')
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid,lock=True)
            current=await self._restore(cursor,tree,node_id)
            if current.node.status=='generating':
                raise ValueError('活动任务应通过普通检查点保存生成')
            known={event.id:event for event in current.events}
            if generation.id in known:
                if not self._same_event(known[generation.id],generation):
                    raise QuestionTreeConflict('私有生成标识对应不同内容')
                return current
            await self._insert_event(cursor,generation.model_copy(update={'sequence':len(current.events)+1}))
            # 仅增加私有记录，不改变公开上下文版本，避免旧请求的审计使新任务无故冲突。
            return await self._restore(cursor,tree,node_id)

    async def load_agent_context(self,tree_id,owner_userid,selected_node_id=None):
        """一致性读取有效路径及已接受交互；不载入原生推理、未来摘要或其他分支。"""
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid)
            if tree.kind=='assistant':
                if selected_node_id is not None:
                    raise ValueError('助理不接受节点选择')
                from app.repository.assistant_memory import load_memory
                memory=await load_memory(self.trees,cursor,tree)
                if memory.nodes and memory.nodes[-1].status=='generating':
                    raise ValueError('助理当前任务尚未结束')
                return tree,memory.path,memory.writes,memory.workspace,tuple(m for m in memory.messages if m.kind=='intermediate')
            else:
                snapshot=await self.trees._snapshot(cursor,tree)
                if selected_node_id is None:
                    if snapshot.nodes:
                        raise ValueError('非空树必须显式选择继续提问的节点')
                    return tree,None,(),None,()
                path=snapshot.context_path(selected_node_id)
            ids=[node.id for node in path.nodes]
            marks=','.join(['%s']*len(ids))
            await cursor.execute('SELECT node_id,content FROM question_node_contents WHERE node_id IN ('+marks+')',ids)
            contents={row['node_id']:_decode(row['content']) for row in await cursor.fetchall()}
            if set(contents)!=set(ids):
                raise ValueError('上下文正文缺失')
            writes=tuple(NodeWrite(node=node,content=contents[node.id]) for node in path.nodes)
            if path.summary is not None and tree.kind!='assistant':
                source=snapshot.context_path(path.summary.parent_id)
                if writes[0].content.covered_from_id!=source.nodes[0].id:
                    raise ValueError('摘要覆盖起点与其祖先路径不一致')
            await cursor.execute("SELECT * FROM question_node_messages WHERE kind='intermediate' AND status='completed' AND node_id IN ("+marks+
                ') ORDER BY node_id,sequence',ids)
            messages=tuple(_row(NodeMessage,row) for row in await cursor.fetchall())
            workspace=await self.trees._workspace(cursor,tree_id,path.workspace_version_id)
            if workspace is None:
                raise ValueError('所选节点缺少工作区快照')
            return tree,path,writes,workspace,messages

    async def find_file(self,tree_id,owner_userid,file_id) -> AttachmentReference:
        # 先校验格式，再校验树归属；UUID 只作引用，不能代替文件访问授权。
        file_id=TypeAdapter(FileID).validate_python(file_id)
        async with self.trees._transaction() as cursor:
            await self.trees._owned_tree(cursor,tree_id,owner_userid)
            await cursor.execute('SELECT c.content FROM question_node_contents AS c '
                'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id=%s AND n.kind=\'task\' '
                'AND JSON_CONTAINS(c.content,%s,\'$.attachments\')=1',
                (tree_id,_dump([{'file_id':file_id}])))
            found=[]
            for row in await cursor.fetchall():
                content=_CONTENT.validate_python(_decode(row['content']))
                found.extend(item for item in content.attachments if item.file_id==file_id)
            if not found:raise QuestionFileNotFound('本树附件不存在')
            if any(item!=found[0] for item in found):raise ValueError('同一文件 UUID 对应的类型不一致')
            return found[0]

    async def _insert_event(self,cursor,event):
        if isinstance(event,NodeMessage):
            await cursor.execute('INSERT INTO question_node_messages(id,node_id,kind,status,text,sequence,created_at) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s)',(event.id,event.node_id,event.kind,event.status,event.text,event.sequence,_date(event.created_at)))
            if event.status!='completed':
                return
            # 消息与待向量化记录共事务保存；不发模型请求，不重复保存原文。
            await cursor.execute('INSERT INTO question_node_memories '
                '(id,node_id,source_id,kind,embedding_status,created_at) VALUES(%s,%s,%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE id=question_node_memories.id',
                (new_tree_id(),event.node_id,event.id,event.kind,'pending',_date(event.created_at)))
        else:
            await cursor.execute('INSERT INTO question_node_generations(id,node_id,status,payload,sequence,created_at) '
                'VALUES(%s,%s,%s,%s,%s,%s)',(event.id,event.node_id,event.status,
                _dump(event.payload.model_dump(mode='json')),event.sequence,_date(event.created_at)))

    @staticmethod
    def _same_event(left,right):
        return type(left) is type(right) and left.model_dump(exclude={'sequence','created_at'})==right.model_dump(exclude={'sequence','created_at'})

    async def checkpoint(self,tree_id,owner_userid,node_id,*,expected_revision:int,
                         events=(),workspace:WorkspaceVersion | None=None,exchanges:list[dict] | None=None,tool_results:dict | None=None) -> RestoredNode:
        return await self._commit(tree_id,owner_userid,node_id,expected_revision=expected_revision,
            events=events,workspace=workspace,exchanges=exchanges,tool_results=tool_results)

    async def finish_node(self,tree_id,owner_userid,node_id,*,status:str,expected_revision:int,
                          final:NodeMessage | None=None,events=(),workspace:WorkspaceVersion | None=None,
                          exchanges:list[dict] | None=None,tool_results:dict | None=None) -> RestoredNode:
        if status not in ('complete','failed','cancelled'):
            raise ValueError('节点终态不合法')
        if (status=='complete')!=(final is not None):
            raise ValueError('完成节点必须提交最终消息；失败或取消不伪造最终输出')
        if final is not None and (final.kind!='final' or final.node_id!=node_id or final.status!='completed'):
            raise ValueError('最终消息与节点不一致')
        return await self._commit(tree_id,owner_userid,node_id,expected_revision=expected_revision,
            events=(*events,final) if final else events,workspace=workspace,exchanges=exchanges,terminal=status,tool_results=tool_results)

    async def _commit(self,tree_id,owner_userid,node_id,*,expected_revision,events,workspace,exchanges,terminal=None,latest=False,tool_results=None):
        copied=[]
        for event in events:
            if not isinstance(event,(NodeMessage,NodeGeneration)):
                raise ValueError('执行记录类型不合法')
            copied.append(type(event).model_validate(event.model_dump()))
        if workspace is not None:
            workspace=WorkspaceVersion.model_validate(workspace.model_dump())
            if workspace.tree_id!=tree_id:
                raise ValueError('工作区版本不能跨树')
        if exchanges is not None:
            exchanges=TaskContent(question='校验占位',exchanges=exchanges).exchanges
        if tool_results is not None:
            tool_results=TaskContent(question='校验占位',tool_results=tool_results).tool_results
        if len({event.id for event in copied})!=len(copied):
            raise ValueError('同批执行记录标识重复')
        async with self.trees._transaction() as cursor:
            tree=await self.trees._owned_tree(cursor,tree_id,owner_userid,lock=True)
            current=await self._restore(cursor,tree,node_id)
            known={event.id:event for event in current.events}
            new=[]
            for event in copied:
                if event.node_id!=node_id:
                    raise ValueError('执行记录不能跨节点')
                existing=known.get(event.id)
                if existing is not None:
                    if not self._same_event(existing,event):
                        raise QuestionTreeConflict('已提交的执行记录不能覆盖')
                    continue
                if event.sequence is not None:
                    raise ValueError('新执行记录序号由仓库分配')
                if (isinstance(event,NodeMessage) and event.kind not in
                        (('intermediate','final') if terminal=='complete' or event.status=='interrupted' else ('intermediate',))):
                    raise ValueError('消息类型不适用于本次提交')
                new.append(event)
            if sum(isinstance(event,NodeMessage) and event.kind=='final' and event.status=='completed' for event in new)>1:
                raise ValueError('节点只能有一条最终消息')
            original=current.content.exchanges
            changed_exchanges=False
            if exchanges is not None and exchanges!=original:
                if exchanges[:len(original)]==original:
                    changed_exchanges=True
                elif original[:len(exchanges)]!=exchanges:
                    raise QuestionTreeConflict('已接受的交互轨迹不能改写')
                # 回执丢失后重放较早的完整前缀，不倒退或删除后续轨迹。
            merged_results = dict(current.content.tool_results)
            if tool_results is not None:
                accepted_exchanges = exchanges if changed_exchanges else original
                calls = {call['id']: call['function']['name'] for message in accepted_exchanges
                         if message.get('role') == 'assistant' for call in message.get('tool_calls', [])}
                receipts = {message['tool_call_id'] for message in accepted_exchanges if message.get('role') == 'tool'}
                for call_id, value in tool_results.items():
                    if calls.get(call_id) != value.name or call_id not in receipts:
                        raise ValueError('结构化结果必须对应已接受的工具调用与回执')
                    if call_id in merged_results and merged_results[call_id] != value:
                        raise QuestionTreeConflict('已提交的工具结构化结果不能覆盖')
                    merged_results[call_id] = value
            changed_results = merged_results != current.content.tool_results
            changed_workspace=workspace is not None and workspace.id!=current.workspace.id
            if workspace is not None and not changed_workspace and workspace.content!=current.workspace.content:
                raise QuestionTreeConflict('已有工作区版本不能覆盖')
            changed_terminal=terminal is not None and terminal!=current.node.status
            changed=bool(new) or changed_exchanges or changed_workspace or changed_terminal or changed_results
            if not changed:
                return current  # 相同记录即使使用旧 revision 重放也不重复提交。
            if not latest:
                self.trees._revision(tree,expected_revision)
            if current.node.status!='generating':
                raise QuestionTreeConflict('已结束节点拒绝迟到写入')
            snapshot = await self.trees._snapshot(cursor, tree) if tree.kind == 'exploration' else None
            assistant_memory = None
            if tree.kind == 'assistant':
                from app.repository.assistant_memory import load_memory, commit_memory
                assistant_memory = await load_memory(self.trees,cursor,tree)
                if node_id != tree.main_leaf_id:
                    raise QuestionTreeConflict('助理仅允许更新当前末端任务')
            if changed_workspace and tree.kind == 'assistant':
                await cursor.execute('UPDATE assistant_workspaces SET id=%s,content=%s,created_at=%s,updated_at=%s WHERE tree_id=%s',
                    (workspace.id,_dump(workspace.content),_date(workspace.created_at),_date(utc_now()),tree_id))
            elif changed_workspace:
                await cursor.execute('SELECT id FROM question_workspaces WHERE id=%s',(workspace.id,))
                if await cursor.fetchone() is not None:
                    raise QuestionTreeConflict('检查点必须使用新的工作区版本，不能回退旧版本')
                await self.trees._insert_workspaces(cursor,tree_id,(workspace,))
            sequence=len(current.events)
            for event in new:
                sequence+=1
                await self._insert_event(cursor,event.model_copy(update={'sequence':sequence}))
            final_messages=[event for event in (*current.messages,*new) if isinstance(event,NodeMessage)
                and event.kind=='final' and event.status=='completed']
            # 检查点只更新生成轨迹与答案，保留创建时登记的原始附件。
            content=current.content.model_copy(update={
                'exchanges':exchanges if changed_exchanges else original,
                'tool_results':merged_results,
                'answer':final_messages[0].text if final_messages else ''})
            status=terminal or current.node.status
            if (status=='complete')!=bool(final_messages):
                raise ValueError('最终消息必须与完成终态共同提交')
            version_id=None if tree.kind=='assistant' else (workspace.id if changed_workspace else current.workspace.id)
            await cursor.execute('UPDATE question_node_contents SET content=%s WHERE node_id=%s',
                (_dump(content.model_dump(mode='json')),node_id))
            now=utc_now()
            await cursor.execute('UPDATE question_nodes SET status=%s,workspace_version_id=%s,finished_at=%s WHERE id=%s',
                (status,version_id,_date(now) if terminal else None,node_id))
            await cursor.execute('UPDATE question_trees SET revision=revision+1,updated_at=%s WHERE id=%s',(_date(now),tree_id))
            tree=tree.model_copy(update={'revision':tree.revision+1,'updated_at':now})
            if self.commit_hook is not None:
                await self.commit_hook(cursor,tree,node_id,content,status)
            restored = await self._restore(cursor,tree,node_id)
            if snapshot is not None:
                self.trees._update_snapshot(cursor, snapshot, tree, restored.node)
            if assistant_memory is not None:
                await commit_memory(self.trees,cursor,assistant_memory,restored,
                    new_exchanges=exchanges[len(original):] if changed_exchanges else ())
            return restored

    async def load_memory_source(self,tree_id,owner_userid,memory_id) -> NodeMessage:
        """记忆命中只按已认证用户和精确消息引用回查，不从其他节点猜测原文。"""
        async with self.trees._transaction() as cursor:
            await self.trees._owned_tree(cursor,tree_id,owner_userid)
            await cursor.execute('SELECT s.* FROM question_node_messages AS s '
                'JOIN question_nodes AS n ON n.id=s.node_id '
                'JOIN question_node_memories AS m ON m.node_id=s.node_id AND m.kind=s.kind AND m.source_id=s.id '
                "WHERE n.tree_id=%s AND m.id=%s AND s.status='completed' "
                "AND (n.status<>'cancelled' OR s.kind='question')",(tree_id,memory_id))
            row=await cursor.fetchone()
            if row is None:
                raise ValueError('记忆来源消息不存在或不属于本树')
            return _row(NodeMessage,row)
