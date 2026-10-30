"""助理角色与 Core 通用规则的唯一组合入口。"""
from app.agent.core.prompt import build_system_prompt as compose
from .role import ASSISTANT_ROLE_PROMPT
from .personal_information import ASSISTANT_PERSONAL_INFORMATION_PROMPT
from .portrait import ASSISTANT_PORTRAIT_PROMPT


def build_system_prompt(*,tool_call_template=''):
    role='\n\n'.join((ASSISTANT_ROLE_PROMPT,ASSISTANT_PERSONAL_INFORMATION_PROMPT,ASSISTANT_PORTRAIT_PROMPT))
    return compose(role=role,tool_call_template=tool_call_template,
        output_guidance='个人助理的 finish_task 必须明确填写 send_to_user。true 将最终正文发送给所属用户，false 仅保存结论并结束本轮；接收用户由程序绑定。提醒消息先交给助理判断，不会自动通知用户。需要向其他助理沟通时，仅使用实际提供的 send_assistant_message，不以 finish_task 代替。比如内部整理完成且无须通知时，填写 content="已完成整理"、send_to_user=false；需要答复用户时填写完整正文并选择 true。')

__all__=['ASSISTANT_ROLE_PROMPT','ASSISTANT_PERSONAL_INFORMATION_PROMPT','ASSISTANT_PORTRAIT_PROMPT','build_system_prompt']
