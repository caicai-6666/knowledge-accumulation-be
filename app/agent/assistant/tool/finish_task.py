"""助理最终输出：结束任务并明确选择是否通知所属用户。"""
from pydantic import Field, field_validator, model_validator
from typing import Annotated
from textwrap import dedent
from app.model.question_file import FileID
from app.agent.core.tool import ToolExecutionResult
from app.agent.core.tool.feedback import with_feedback
from app.agent.core.tool.hooks import FeedbackOnlyHook
from app.service.assistant_file_delivery import FileDeliveryRejected
from dataclasses import replace
from app.auth.model import AuthenticatedUser
from app.agent.core.json_codec import ModelActionRejected
from app.agent.core.workspace import WorkspaceText
from app.agent.core.tool.base import FinishTaskTool, OutputArguments


class FinishAssistantTaskArguments(OutputArguments):
    content: WorkspaceText = Field(description='本轮完整最终反馈：有依据的答案、已完成范围与具体限制，或需要用户回答的澄清问题。send_to_user 为 true 时向所属用户发送此正文；为 false 时应写成本轮内部处理结论，保留事实、执行结果、未完成事项及限制。因无法通知而改为 false 时，检查并调整面向员工的称呼、提醒和提问，将需要确认的内容记为待确认事项，不暗示已通知或收到确认；不提交私有推理，不将部分完成说成全部完成。')
    send_to_user: bool = Field(strict=True,description='是否将 content 发送给所属用户，必须明确选择 true 或 false。先核对本轮所属用户信息中的在职状态：所属员工已离职时必须选 false，包括答复、提醒、澄清及风险告知，仅保存结论；所属员工在职且需要阅读答复、提醒或澄清问题时选 true，其他内部记录选 false。程序会拒绝离职或未绑定可信状态时的 true；按反馈检查并调整 content 为内部处理结论，再改为 false 后重新提交。例如提醒到期后没有需要通知的事项，可以选 false。')

    file_ids: list[Annotated[FileID, Field(description='所属用户上传附件的完整 UUID，不接受注册中心文件 ID。')]] = Field(
        default_factory=list, max_length=10,
        description='可选，随正文分别发送给所属用户的附件 ID，最多10个；仅限当前助理可访问的本人上传 PDF，每份不超过20 MB。省略为空，重复ID按首次出现去重。send_to_user=false 时必须为空；发送的是已保存的附件，图片转换后按PDF发送，不接受路径或注册中心文件ID。')

    @field_validator('file_ids')
    @classmethod
    def unique_files(cls, values):
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def silent_files(self):
        if self.file_ids and not self.send_to_user:
            raise ModelActionRejected('send_to_user=false 时不能附带文件，本次最终结果未提交。',
                '如仅保存内部结论，请清空 file_ids 并调整正文，不声称已发送；确需通知且员工在职时再选择 true。')
        return self


def render_assistant_finish(arguments, execution, context):
    if execution.result.status == 'succeeded':
        delivery = '正文已登记交付' if arguments.send_to_user else '仅保存内部结论，不通知用户'
        text = dedent("""
            ### 任务结束

            状态：本轮最终正文已保存，任务已结束。
            用户通知：{delivery}
            附件交付：{count} 份
        """).strip().format(delivery=delivery, count=len(arguments.file_ids))
        if arguments.file_ids:
            text += '\n\n已登记的附件 ID：\n' + '\n'.join('- ' + value for value in arguments.file_ids)
        return with_feedback(execution, text)
    text = dedent("""
        ### 任务结束未提交

        状态：正文及附件均未登记发送
        原因：{reason}
        下一步：{required_action}
    """).strip().format(**execution.result.tool_result)
    return with_feedback(execution, text)


class AssistantFinishTask(FinishTaskTool):
    def __init__(self, *, user: AuthenticatedUser | None = None, hooks=(), validate_files=None):
        super().__init__(hooks=hooks)
        self.validate_files=validate_files
        # 每轮从可信身份复制状态，不解析模型上下文，也不在共享工具上修改用户归属。
        self.employment_status = (AuthenticatedUser.model_validate(user.model_dump()).employment_status
                                  if user is not None else None)

    def build_registration(self):
        def validate_delivery(name, raw, workspace, arguments):
            if arguments.send_to_user and self.employment_status == 'resigned':
                raise ModelActionRejected(
                    'send_to_user=true 不符合交付规则：所属员工已离职，不能向员工本人发送消息。本次最终结果尚未提交，未进行格式转换或发送。',
                    '先检查并调整 content，使其适合作为本轮内部处理结论：保留有依据的事实、已执行结果、未完成事项和限制；'
                    '将面向员工的称呼、提醒或直接提问改为客观记录，需员工确认的内容记为待确认事项，不编造答复或授权。'
                    '例如“请您确认会议时间”可改为“会议时间待确认，本轮未通知员工”。'
                    '调整正文后，将 send_to_user 改为 false 并重新调用 finish_task，仅保存结论并结束任务。'
                    '如需回复其他助理，仍可使用 send_assistant_message；不要把未发送的通知描述为已送达。')
            if arguments.send_to_user and self.employment_status is None:
                # 独立 Core 未绑定身份时不能默认在职放行；静默保存仍可用。
                raise ModelActionRejected(
                    '尚未绑定所属员工的可信在职状态，不能确认允许发送。本次最终结果尚未提交，未进行格式转换或发送。',
                    '先检查并调整 content 为内部处理结论，保留事实、执行结果及限制，将面向员工的提醒或提问改为待办、待确认事项，并说明无法确认通知条件。'
                    '将 send_to_user 改为 false 后重新提交；不要猜测员工状态、编造确认或声称已发送。')
            return workspace
        async def execute(arguments):
            if arguments.file_ids:
                if self.validate_files is None:
                    return ToolExecutionResult(status='failed', tool_result={
                        'reason':'附件交付暂不可用，本次未提交。',
                        'required_action':'请稍后重试，或清空 file_ids 并在正文中明确未发送附件。'})
                try:
                    await self.validate_files(arguments.file_ids)
                except FileDeliveryRejected as error:
                    return ToolExecutionResult(status='failed', tool_result={
                        'reason':error.reason, 'required_action':error.action})
                except Exception:
                    return ToolExecutionResult(status='failed', tool_result={
                        'reason':'附件发送前的检查暂不可用，本次未提交。',
                        'required_action':'请稍后重试；若清空 file_ids 继续结束，需调整正文说明附件未发送。'})
            return ToolExecutionResult(status='succeeded',tool_result={'status':'accepted'})
        entry=super().build_registration()
        return replace(entry, handler=validate_delivery, executor=execute,
            hooks=(*self.hooks, FeedbackOnlyHook(render_assistant_finish)))

    arguments_model = FinishAssistantTaskArguments
    description = """当你需要提交最终答复、记录本轮处理结论，或因缺少必要信息而需要用户补充后才能继续时，使用这个工具。提交完整反馈并结束本轮任务，成功后不再继续本轮调用。

    通过 send_to_user 明确选择是否向所属用户发送正文。依据本轮所属用户信息中的在职状态，所属员工已离职时必须选择 false，仅保存结论，不发送答复、提醒、澄清或风险通知；例如离职员工的提醒处理完毕，用 send_to_user=false 收尾。所属员工在职时，需要用户回答澄清问题选择 true，无须通知用户选择 false。提醒到期后也由你根据处理结果决定是否通知，而不是每次提醒都必须发送消息。

    程序按可信员工状态校验发送选择；员工已离职或状态未绑定时，send_to_user=true 会被拒绝且不会提交或发送。按反馈检查并调整 content 为内部处理结论，保留事实、结果及未完成事项，将直接提醒或提问改为待办、待确认记录，再以 send_to_user=false 重新提交；不要仅修改发送开关而忽略正文用途。

    需要随回复交付用户附件时，提供 file_ids，例如用户要求取回已上传合同，可填写其完整 UUID；支持最多10份、不超过20 MB的PDF，按保存后的文件发送。成功表示已登记交付，不代表钉钉已送达。文件无法交付时按反馈修正引用和正文，不自动公开文件。

    需要求助或回复其他助理时，先调用 send_assistant_message；本工具不能向其他助理发送消息。不把失败、待处理或尚未核实的事项写成已经完成。"""
    deliver_output = staticmethod(lambda arguments: arguments.send_to_user)
