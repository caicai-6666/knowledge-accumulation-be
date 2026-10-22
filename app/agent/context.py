"""树持久化与模式上下文之间的有效投影；Core 无需依赖树或数据库。"""
from app.agent.core import ContextInjection,HistoryTurn
from app.agent.core.workspace import read_workspace
from app.agent.core.native_messages import messages_from_exchanges


def build_tree_context(writes,workspace,outputs,*,supplemental=''):
    history=[]
    summary=None
    for write in writes:
        if write.node.kind=='summary':
            summary=write.content.text
        else:
            history.append(HistoryTurn(question=write.content.question,attachments=write.content.attachments,answer=write.content.answer,
                native_messages=messages_from_exchanges(write.content.exchanges) if write.content.exchanges else None,
                status=write.node.status,intermediate=[message.text for message in outputs
                    if message.node_id==write.node.id and message.status=='completed']))
    return ContextInjection(summary=summary,turns=history,
        workspace=read_workspace(workspace.content if workspace else {}),supplemental=supplemental)
