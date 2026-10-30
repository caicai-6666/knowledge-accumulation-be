"""权威提交与输出展示分离；内存宿主和节点仓库使用同一运行协议。"""
from copy import deepcopy
from app.model.node_persistence import GenerationPayload, NodeGeneration, NodeMessage
from app.model.question_tree import WorkspaceVersion, new_tree_id
from app.repository.question_tree import QuestionTreeConflict


class MemoryJournal:
    def __init__(self,node_id=None):
        self.node_id=node_id or new_tree_id()
        self.generations=[]
        self.audit=[]
        self.exchanges=[]
        self.outputs=[]
        self.workspace=None
        self.status='generating'

    async def record_generation(self,completion=None,*,status='complete',purpose='main',raw_chunks=(),error_type=None):
        if completion is not None:
            event=NodeGeneration.from_completion(self.node_id,completion)
            event=event.model_copy(update={'payload':event.payload.model_copy(update={'purpose':purpose})})
        else:
            event=NodeGeneration(node_id=self.node_id,status=status,payload=GenerationPayload(
                purpose=purpose,raw_chunks=deepcopy(list(raw_chunks)),error_type=error_type))
        self.generations.append(event)
        await self._save_generation(event)

    async def _save_generation(self,event):
        pass

    async def record_payload(self,status,payload):
        """执行后钩子的原生生成写入当前任务审计，转换正文仍经普通 commit 提交。"""
        event=NodeGeneration(node_id=self.node_id,status=status,payload=payload).model_copy(deep=True)
        self.generations.append(event)
        await self._save_generation(event)

    async def record_expert(self, event):
        """完整 Responses 及提交状态保留在私有生成中，不作为主助手历史回传。"""
        response = deepcopy(event.get('response'))
        state = event['status']
        messages = [item for item in (response or {}).get('output', ()) if item.get('type') == 'message']
        generation = NodeGeneration(node_id=self.node_id,
            status='complete' if state == 'success' else 'cancelled' if state == 'cancelled' else 'failed',
            payload=GenerationPayload(purpose='external_expert',
                assistant_message=messages[-1] if messages else None, raw_response=response,
                raw_chunks=[deepcopy(event)], usage=(response or {}).get('usage') or {},
                finish_reason=(response or {}).get('status'), error_type=event.get('error_type')))
        self.generations.append(generation)
        await self._save_generation(generation)

    async def record_web_refinement(self, event):
        """网页精炼原生响应完整保存；模型输出校验失败也不截断或丢弃审计。"""
        message = event.get('assistant_message')
        generation = NodeGeneration(node_id=self.node_id,
            status='complete' if message is not None else 'cancelled' if event.get('status') == 'cancelled' else 'failed',
            payload=GenerationPayload(purpose='web_page_refinement', assistant_message=deepcopy(message),
                raw_response=deepcopy(event.get('response')), raw_chunks=[deepcopy(event)],
                usage=event.get('usage') or {}, finish_reason=event.get('finish_reason'),
                error_type=event.get('error_type')))
        self.generations.append(generation)
        await self._save_generation(generation)

    async def record_file_retrieval(self, event):
        """保留子检索完整反馈，成功后的模型上下文可以清除连续失败轨迹。"""
        generation = NodeGeneration(node_id=self.node_id, status='complete',
            payload=GenerationPayload(purpose='file_retrieval',
                assistant_message=deepcopy(event['assistant_message']), raw_response=deepcopy(event)))
        self.generations.append(generation)
        await self._save_generation(generation)

    async def commit(self,*,workspace,exchanges,output=None,final=False,output_id=None):
        if self.status!='generating':
            raise RuntimeError('运行已结束')
        self.workspace=workspace.model_copy(deep=True)
        self.exchanges=deepcopy(exchanges)
        if output is not None:
            self.outputs.append(('final' if final else 'intermediate',output))
        if final:
            self.status='complete'

    async def abort(self,status):
        if status not in ('cancelled','failed'):
            raise ValueError('停止原因必须是取消或失败；替代必须由原子登记操作完成')
        if self.status=='generating':
            self.status=status


class NodeJournal(MemoryJournal):
    def __init__(self,repository,restored,*,cancellation_status=None):
        super().__init__(restored.node.id)
        self.repository=repository
        self.restored=restored
        self.owner=restored.tree.owner_userid
        self.tree_id=restored.tree.id
        self.status=restored.node.status
        self.exchanges=deepcopy(restored.content.exchanges)
        self.cancellation_status=cancellation_status

    async def _save_generation(self,event):
        try:
            self.restored=await self.repository.checkpoint(self.tree_id,self.owner,self.node_id,
                expected_revision=self.restored.tree.revision,events=(event,))
        except QuestionTreeConflict:
            latest=await self.repository.load_node(self.tree_id,self.owner,self.node_id)
            if latest.node.status=='generating':
                raise
            # 外部控制已先提交终态：原生响应仍保存私有审计，但绝不执行其工具或公开输出。
            self.restored=await self.repository.append_terminal_generation(self.tree_id,self.owner,self.node_id,event)
            self.status=self.restored.node.status

    async def commit(self,*,workspace,exchanges,output=None,final=False,output_id=None):
        if self.status!='generating':
            raise RuntimeError('运行已结束')
        version=None
        if workspace.model_dump()!=self.restored.workspace.content:
            version=WorkspaceVersion(tree_id=self.tree_id,content=workspace.model_dump(mode='json'))
        event=NodeMessage(node_id=self.node_id,kind='final' if final else 'intermediate',text=output,
            **({'id':output_id} if output_id is not None else {})) if output is not None else None
        kwargs={'expected_revision':self.restored.tree.revision,'workspace':version,'exchanges':exchanges}
        if final:
            if event is None:
                raise ValueError('最终提交必须包含公开消息')
            updated=await self.repository.finish_node(self.tree_id,self.owner,self.node_id,status='complete',final=event,**kwargs)
        else:
            updated=await self.repository.checkpoint(self.tree_id,self.owner,self.node_id,events=(event,) if event else (),**kwargs)
        self.restored=updated
        # 数据库确认后才更新内存和发布回执；提交不确定时上抛并停止，不重放。
        await super().commit(workspace=workspace,exchanges=exchanges,output=output,final=final,output_id=output_id)

    async def abort(self,status):
        if status=='cancelled' and self.cancellation_status is not None:
            status=self.cancellation_status()
        if self.status=='generating':
            try:
                self.restored=await self.repository.finish_node(self.tree_id,self.owner,self.node_id,status=status,
                    expected_revision=self.restored.tree.revision)
            except QuestionTreeConflict:
                self.restored=await self.repository.load_node(self.tree_id,self.owner,self.node_id)
                if self.restored.node.status=='generating':
                    raise
            self.status=self.restored.node.status
        await super().abort(status)
