"""助理间文字通信工具；发送身份与交付由可信宿主绑定。"""
from textwrap import dedent
from pydantic import Field
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult
from app.agent.core.tool.progress import DEFAULT_TOOL_PROGRESS
from app.agent.core.tool.feedback import with_feedback


def render_send_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 助理消息发送

            状态：发送请求未登记
            原因及处理方向：{message}
        """).strip().format(**result)
    else:
        # 登记与检查点同事务提交；刷新交付不等于对方已经收到或处理。
        text = dedent("""
            ### 助理消息发送

            状态：发送请求已登记
            接收用户 ID：{recipient_userid}
            处理进度：尚不能确认对方已收到、处理或答复。
        """).strip().format(recipient_userid=arguments.recipient_userid)
    return with_feedback(execution, text)


class SendAssistantMessageArguments(WorkspaceObject):
    """当你需要向另一个用户的助理求助、协作或回复其消息时，使用这个工具。向已确认的对象发送一条文字消息；例如请对方协助确认会议时间，并说明相关背景和希望得到的答复。

    提供接收用户的准确 userid 和消息正文。目标员工在职或离职均可联系其助理，不因离职状态拒绝正常协作；例如离职员工的助理仍可协助核对历史事项。只有用户名时，先用 search_assistants 查询，再用 view_assistant_query 查看并确认对象；不凭姓名猜测 userid。对方不能直接看到你当前的完整对话和工作区，消息中应附上理解请求所需的背景。

    接收方以本工具 recipient_userid 为准，代收或转发者也必须有权限。检查carol通过不能把正文先发给bob转交；若仅carol有权，只能直接发给已确认userid的carol，不把摘要交给bob，也不预先承诺如此转交。

    发送前识别正文涉及的全部平台文件来源，并用 check_file_disclosure 对实际接收用户逐份检查。只有允许告知的来源才可转述，摘要、建议和数字确认也适用；来源不明、检查失败或工具不可用时不发送疑似受限部分。拒绝正文及格式示例也不能包含受限真实值或其翻译、编码、变换，只用字段名称解释限制，也不得建议找高权限同事代传或换群、换通道绕过实际接收方权限。普通常识交流无需文件检查。

    成功反馈表示发送请求已受理，不代表对方已经处理或答复；目标不可用时核对对象，失败时不宣称发送成功。这个工具不直接通知用户本人，也不结束本轮，后续仍需调用 finish_task。
    """
    recipient_userid: str = Field(strict=True,min_length=1,max_length=128,pattern=r'^\S(?:.*\S)?$',
        description='接收消息的助理所属用户的准确 userid，区分大小写；原样使用已确认的查询结果或来源消息中的用户标识，不填用户名，也不猜测；目标员工已离职也可向其助理发送消息，在职状态不决定助理是否可接收。此userid必须与内容告知检查的接收用户一致；代收转发者同样须有权限，不能填未获授权的中转人。')
    message: str = Field(strict=True,min_length=1,max_length=10000,pattern=r'\S',
        description='发送给目标助理的纯文字消息，可包含 Markdown。说明沟通目的、必要依据和希望对方处理的事项，不附带文件、访问令牌或私有推理。文件相关事实及衍生结论须先对实际接收人通过 check_file_disclosure；来源不明或未通过的内容不得写入正文。')


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
        executor=execute,progress=DEFAULT_TOOL_PROGRESS,hooks=(render_send_feedback,))
