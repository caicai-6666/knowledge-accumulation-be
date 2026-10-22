"""助理角色与 Core 通用规则的唯一组合入口。"""
from app.agent.core.prompt import build_system_prompt as compose
from .role import ASSISTANT_ROLE_PROMPT


def build_system_prompt(*,tool_call_template=''):
    return compose(role=ASSISTANT_ROLE_PROMPT,tool_call_template=tool_call_template)

__all__=['ASSISTANT_ROLE_PROMPT','build_system_prompt']
