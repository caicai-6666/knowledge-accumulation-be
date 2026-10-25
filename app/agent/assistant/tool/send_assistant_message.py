"""助理间文字通信工具；发送身份与交付由可信宿主绑定。"""
from pydantic import Field
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult
from app.agent.core.tool.progress import DEFAULT_TOOL_PROGRESS


class SendAssistantMessageArguments(WorkspaceObject):
    """当你需要向另一个用户的助理求助、协作或回复其消息时，使用这个工具。向已确认的对象发送一条文字消息；例如请对方协助确认会议时间，并说明相关背景和希望得到的答复。

    提供接收用户的准确 userid 和消息正文。只有用户名时，先用 search_assistants 查询，再用 view_assistant_query 查看并确认对象；不凭姓名猜测 userid。对方不能直接看到你当前的完整对话和工作区，消息中应附上理解请求所需的背景。

    成功反馈表示发送请求已受理，不代表对方已经处理或答复；目标不可用时核对对象，失败时不宣称发送成功。这个工具不直接通知用户本人，也不结束本轮，后续仍需调用 finish_task。
    """
    recipient_userid: str = Field(strict=True,min_length=1,max_length=128,pattern=r'^\S(?:.*\S)?$',
        description='接收消息的助理所属用户的准确 userid，区分大小写；原样使用已确认的查询结果或来源消息中的用户标识，不填用户名，也不猜测。')
    message: str = Field(strict=True,min_length=1,max_length=10000,pattern=r'\S',
        description='发送给目标助理的纯文字消息，可包含 Markdown。说明沟通目的、必要依据和希望对方处理的事项，不附带文件、访问令牌或私有推理。')


def send_registration(*,recipient_exists,flush):
    async def execute(arguments):
        if not await recipient_exists(arguments.recipient_userid):
            return ToolExecutionResult(status='failed',tool_result={
                'code':'recipient_unavailable','message':'未找到可接收消息的目标用户助理，请核对 userid。'})
        # 此处不发送。节点提交钩子原子登记交付；提交后才刷新该任务的发送记录。
        return ToolExecution(ToolExecutionResult(status='succeeded',tool_result={
            'status':'accepted','message':'已登记助理消息发送请求，不代表对方已处理或答复。'}),after_commit=flush)
    return RegisteredTool('send_assistant_message',
        SendAssistantMessageArguments.__doc__,
        SendAssistantMessageArguments,lambda name,raw,workspace,args:workspace,
        executor=execute,progress=DEFAULT_TOOL_PROGRESS)
