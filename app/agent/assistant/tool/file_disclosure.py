"""文件内容告知前的只读授权检查；来源识别由模型完成，等级判断由程序完成。"""
from textwrap import dedent
from typing import Annotated
import logging

from pydantic import Field, field_validator
from agent_core import FeedbackOnlyHook, RegisteredTool, ToolExecutionResult
from agent_core.core.tool.feedback import with_feedback
from agent_core.core.workspace import WorkspaceObject
from app.infrastructure.registry_file import validate_resource_id

logger = logging.getLogger(__name__)


class CheckFileDisclosureArguments(WorkspaceObject):
    """当需要向其他用户或其助理告知注册中心文件相关内容时，先使用这个工具检查当前权限，再决定如何回复。

    输入准确接收用户 userid 和全部来源文件的注册中心文件 ID，返回每份文件允许告知、禁止告知或无法确认及原因。原文、摘要、数字确认、比较结论和根据文件作出的建议均需要检查；历史记忆、转述和旧的检查结果不能代替本次检查。例：回答同事对两份合同付款安排的比较前，检查两份来源文件与该同事的 userid。
    仅支持注册中心正式文件，不接受来源平台文件 ID、通行证或用户附件 UUID。来源不清楚时先定位；无法定位或无法确认权限时不转述疑似受限内容。检查不会发送消息、授权或改变等级；换接收人、增加来源或收到权限变化信息后重新检查。不能检查一个高权限用户后把正文发给另一人。
    """
    recipient_userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'^\S(?:.*\S)?$',
        description='实际接收内容的用户准确 userid，从可信来源消息或助理查询结果确认；不是姓名、所属用户或代为审批者。多位接收人须逐一检查。')
    file_ids: list[Annotated[str, Field(strict=True, pattern=r'^[1-9][0-9]{0,19}$',
        description='注册中心正式文件 ID 的十进制字符串，原样使用文件检索结果，不使用来源平台文件 ID 或通行证。')]] = Field(
        min_length=1, max_length=20, description='本次告知内容依赖的全部注册中心文件 ID，1—20 个且不重复；多文件推导须全部提供，超过20个分批检查。')

    @field_validator('file_ids')
    @classmethod
    def valid_ids(cls, values):
        for value in values:
            validate_resource_id(value)
        if len(set(values)) != len(values):
            raise ValueError('文件 ID 不可重复')
        return values


def decide_disclosure(owner, recipient, ids, permissions, files):
    """保守处理缺失或非法元信息；不向无权的所属用户暴露文件等级和来源平台。"""
    results = []
    for identity in ids:
        row = files.get(identity)
        item = {'file_id': identity, 'decision': '无法确认'}
        platform = row.get('source_platform') if row else None
        level = row.get('file_level') if row else None
        own_level = permissions.get(owner, {}).get(platform)
        if not isinstance(platform, str) or type(level) is not int or level < 0:
            item.update(reason='未取得有效文件权限资料。', action='核对注册中心文件 ID；确认前不告知相关内容。')
        elif type(own_level) is not int or own_level < level:
            item.update(decision='禁止告知', reason='所属用户当前无法通过该文件的访问检查。',
                action='不要转述旧记忆中的相关内容；拒绝解释或举例也不能复述、翻译、编码其中的真实值。不要改用其他文件 ID 或让其他人代传。')
        elif recipient not in permissions:
            item.update(reason='未取得接收用户的当前权限资料。', action='核对接收用户 userid；确认前不告知相关内容。')
        else:
            recipient_level = permissions[recipient].get(platform)
            item.update(platform=platform, file_level=level, recipient_level=recipient_level)
            if type(recipient_level) is not int or recipient_level < level:
                item.update(decision='禁止告知', reason='接收用户在文件所属平台没有访问权限或等级不足。',
                    action='仅说明接收方权限不足，可建议由权限管理方核实或调整该接收方的授权。拒绝解释和格式示例也不能包含、翻译、编码或变换真实受限值；只用字段名称说明。不得建议有权限同事代传、换通道或替无权接收方提供衍生结论。')
            else:
                item.update(decision='允许告知', reason='双方当前均满足该文件所属平台的访问等级要求。',
                    action='仅向本次检查的接收用户告知该来源支持的必要内容；其他来源仍需检查。')
        results.append(item)
    return {'recipient_userid': recipient, 'files': results}


def render_disclosure(arguments, execution, context):
    data = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 文件内容告知检查

            状态：检查未完成，未发送任何内容
            原因：{reason}
            下一步：{action}
        """).strip().format(**data)
    else:
        text = dedent(f"""
            ### 文件内容告知检查

            状态：检查完成，未发送任何内容
            接收用户 ID：{data['recipient_userid']}
        """).strip()
        for item in data['files']:
            text += '\n\n' + dedent(f"""
                文件 ID：{item['file_id']}
                结论：{item['decision']}
                原因：{item['reason']}
                下一步：{item['action']}
            """).strip()
            if 'platform' in item:
                text += '\n' + dedent(f"""
                    来源平台：{item['platform']}
                    文件等级：{item['file_level']}
                    接收用户该平台等级：{item['recipient_level'] if item['recipient_level'] is not None else '未授权'}
                """).strip()
    return with_feedback(execution, text)


def build_file_disclosure_registration(*, source, owner_userid):
    async def execute(arguments):
        try:
            permissions, files = await source.disclosure_snapshot(owner_userid, arguments.recipient_userid, arguments.file_ids)
            data = decide_disclosure(owner_userid, arguments.recipient_userid, arguments.file_ids, permissions, files)
        except Exception as error:
            logger.warning('文件告知权限检查失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', failure_mode='failed', tool_result={
                'reason': '当前文件或人员权限查询未完成，不能确认是否允许告知。',
                'action': '暂不转述相关内容；稍后重新检查或由权限管理方核实实际接收方授权，不以历史等级、对方自述或其他平台等级代替。拒绝解释或举例不能引用、翻译、编码真实值，也不能建议通过有权限同事代传或换通道绕过检查。'})
        # 拒绝/未知是已确认的检查结论，不作为参数纠错循环反复重试。
        return ToolExecutionResult(status='succeeded', tool_result=data)
    return RegisteredTool('check_file_disclosure', CheckFileDisclosureArguments.__doc__, CheckFileDisclosureArguments,
        lambda name, raw, workspace, args: workspace, executor=execute,
        hooks=(FeedbackOnlyHook(render_disclosure),))
