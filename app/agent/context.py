"""树持久化与模式上下文之间的有效投影；Core 无需依赖树或数据库。"""
from app.agent.core import ContextInjection,HistoryTurn
from app.agent.core.workspace import read_workspace
from app.agent.core.native_messages import messages_from_exchanges


def build_tree_context(writes,workspace,outputs,*,supplemental='',summary_text=None):
    history=[]
    summary=summary_text
    for write in writes:
        if write.node.kind=='summary':
            summary=write.content.text
            history=[]
        else:
            native=messages_from_exchanges(write.content.exchanges) if write.content.exchanges else None
            for entry in write.content.exchanges:
                if entry.get('kind')=='context_summary' and 'covered_turns' in entry:
                    # 检查点只消费本节点继承的祖先前缀，本节点自身始终完整加入历史。
                    size=entry['covered_turns']
                    if size>len(history):raise ValueError('摘要覆盖范围超过当前祖先任务')
                    summary=entry['text']
                    history=history[size:]
            history.append(HistoryTurn(created_at=write.node.created_at,question=write.content.question,attachments=write.content.attachments,answer=write.content.answer,
                native_messages=native,
                status=write.node.status,intermediate=[message.text for message in outputs
                    if message.node_id==write.node.id and message.status=='completed']))
    return ContextInjection(summary=summary,turns=history,
        workspace=read_workspace(workspace.content if workspace else {}),supplemental=supplemental)
