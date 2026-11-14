"""空转心跳裁剪：只在终态事务内删除本轮记录，保留消费账本。"""
from app.agent.assistant.tool.finish_task import FinishAssistantTaskArguments
from app.repository.question_tree import _decode, _date, _dump
from app.model.question_tree import utc_now


async def pruning_rejection(cursor, record, *, terminal=False):
    """执行前和事务内使用同一条件；未知/多轮/整理轨迹默认保留。"""
    reason = None
    if record.task_type != 'heartbeat':
        reason = '当前任务不是心跳，不能裁剪。'
    elif record.file_bindings or record.input_payload.files:
        reason = '当前任务携带附件，不能裁剪其引用记录。'
    else:
        await cursor.execute('SELECT content FROM question_node_contents WHERE node_id=%s', (record.node_id,))
        row = await cursor.fetchone()
        if row is None:
            reason = '当前任务记录不完整，无法确认允许裁剪。'
        else:
            content = _decode(row['content'])
            exchanges = content.get('exchanges', [])
            calls = [call for entry in exchanges for call in entry.get('tool_calls', [])]
            if any(entry.get('kind') == 'context_summary' for entry in exchanges):
                reason = '本轮发生过历史摘要，必须保留摘要及相关记录。'
            elif (not terminal and exchanges) or (terminal and (
                    len(calls) != 1 or calls[0]['function']['name'] != 'finish_task')):
                reason = '本轮已执行其他工具或产生其他交互记录，不属于仅静默结束的空转心跳。'
        if reason is None:
            await cursor.execute("SELECT status,JSON_UNQUOTE(JSON_EXTRACT(payload,'$.purpose')) AS purpose "
                                 "FROM question_node_generations WHERE node_id=%s ORDER BY sequence",
                                 (record.node_id,))
            generations = await cursor.fetchall()
            # 一轮主生成以外的记录包含纠错、失败、子智能体、自动摘要或工作区整理。
            # 不以最后一次成功覆盖此前动作，也不把“先修改后还原”误判为无操作。
            if any(g['purpose'] != 'main' for g in generations):
                reason = '本轮发生过自动整理或其他内部生成，不能裁剪相关记录。'
            elif len(generations) != 1 or generations[0]['status'] != 'complete':
                reason = '本轮存在多次生成、失败或无法确认的执行过程，需要保留记录。'
        if reason is None:
            await cursor.execute('SELECT id FROM assistant_deliveries WHERE task_id=%s LIMIT 1', (record.task_id,))
            if await cursor.fetchone() is not None:
                reason = '本轮已登记对外消息或附件交付，不能裁剪。'
    if reason is None:
        return None
    return {
        'reason': reason + ' 本次最终结果未提交，记录未裁剪。',
        'required_action': '将 prune_current_task 改为 false，保留真实处理结果并重新调用 finish_task；不要重复请求裁剪。',
    }


async def prune_records(repository, cursor, record, restored, initial_workspace):
    """调用者持树和任务行锁；失败全部回滚，不发布半份内存窗口。"""
    from app.repository.assistant_memory import load_memory, make_memory

    calls = [call for entry in restored.content.exchanges for call in entry.get('tool_calls', [])
             if call['function']['name'] == 'finish_task']
    if len(calls) != 1:
        raise ValueError('心跳终态缺少唯一结束动作')
    final = FinishAssistantTaskArguments.model_validate_json(calls[0]['function']['arguments'])
    if not final.prune_current_task:
        return
    rejection = await pruning_rejection(cursor, record, terminal=True)
    if rejection is not None or final.send_to_user or final.file_ids:
        # 工具预检已经给出可纠正反馈；事务阶段不满足表示状态不一致，必须回滚停止。
        raise ValueError('心跳裁剪事务校验未通过')
    from app.agent.core.workspace import read_workspace
    baseline = read_workspace(initial_workspace.content).model_dump(mode='json')
    current = read_workspace(restored.workspace.content).model_dump(mode='json')
    # Core 每轮自动填入当前任务正文；这不是业务编辑，裁剪时也必须移除这份副本。
    current['task_constraints']['task'] = baseline['task_constraints']['task']
    if current != baseline:
        raise ValueError('工作区发生业务变更，拒绝裁剪')
    tree, node = restored.tree, restored.node
    if tree.kind != 'assistant' or tree.main_leaf_id != node.id:
        raise ValueError('只能裁剪当前助理末端心跳')
    memory = await load_memory(repository.trees, cursor, tree)
    writes = tuple(write for write in memory.writes if write.node.id != node.id)
    updated_tree = tree.model_copy(update={'main_leaf_id': node.parent_id})
    first = writes[0].node.sequence if writes else 1
    # 保守策略已排除本轮压缩；既有摘要和工作区直接保留，不重新计算或倒退。
    updated_memory = make_memory(updated_tree, writes, memory.messages, initial_workspace, memory.summary, first)
    await repository._save(cursor, record.model_copy(update={'node_id': None}))
    await cursor.execute('UPDATE question_trees SET main_leaf_id=%s WHERE id=%s', (node.parent_id, tree.id))
    for table in ('question_node_memories', 'question_node_messages', 'question_node_generations', 'question_node_contents'):
        await cursor.execute('DELETE FROM ' + table + ' WHERE node_id=%s', (node.id,))
    await cursor.execute('DELETE FROM question_nodes WHERE id=%s AND tree_id=%s', (node.id, tree.id))
    await cursor.execute('UPDATE assistant_workspaces SET id=%s,content=%s,created_at=%s,first_sequence=%s,updated_at=%s WHERE tree_id=%s',
                         (initial_workspace.id, _dump(initial_workspace.content), _date(initial_workspace.created_at),
                          first, _date(utc_now()), tree.id))
    repository.trees._stage_assistant_memory(cursor, updated_memory)
