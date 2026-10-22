"""探索角色与通用交互、工作区规则组合。"""
from app.agent.core.prompt import build_system_prompt as compose
from .role import EXPLORATION_ROLE_PROMPT


def build_system_prompt(*,tool_call_template=''):
    return compose(role=EXPLORATION_ROLE_PROMPT,tool_call_template=tool_call_template)

__all__=['EXPLORATION_ROLE_PROMPT','build_system_prompt']
