"""助理最终输出：结束任务并明确选择是否通知所属用户。"""
from pydantic import Field
from app.agent.core.workspace import WorkspaceText
from app.agent.core.tool.base import FinishTaskTool, OutputArguments


class FinishAssistantTaskArguments(OutputArguments):
    content: WorkspaceText = Field(description='本轮完整最终反馈：有依据的答案、已完成范围与具体限制，或需要用户回答的澄清问题。send_to_user 为 true 时向所属用户发送此正文，为 false 时仅保留为本轮结论；不提交私有推理，不将部分完成说成全部完成。')
    send_to_user: bool = Field(strict=True,description='是否将 content 发送给所属用户，必须明确选择 true 或 false。需要用户阅读答复、提醒或澄清问题时选 true；仅需内部记录时选 false。例如提醒到期后没有需要通知的事项，可以选 false。')


class AssistantFinishTask(FinishTaskTool):
    arguments_model = FinishAssistantTaskArguments
    description = """当你需要提交最终答复、记录本轮处理结论，或因缺少必要信息而需要用户补充后才能继续时，使用这个工具。提交完整反馈并结束本轮任务，成功后不再继续本轮调用。

    通过 send_to_user 明确选择是否向所属用户发送正文；需要用户回答澄清问题时选择 true，无须通知用户时选择 false。提醒到期后也由你根据处理结果决定是否通知，而不是每次提醒都必须发送消息。

    需要求助或回复其他助理时，先调用 send_assistant_message；本工具不能向其他助理发送消息。不把失败、待处理或尚未核实的事项写成已经完成。"""
    deliver_output = staticmethod(lambda arguments: arguments.send_to_user)
