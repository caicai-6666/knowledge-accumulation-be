"""主助手稳定提示词公共入口：角色、交互、工作区按固定顺序装配。"""
from .role import AGENT_CORE_ROLE_PROMPT
from .interaction import AGENT_CORE_INTERACTION_PROMPT_TEMPLATE,build_agent_core_interaction_prompt
from .workspace import AGENT_CORE_WORKSPACE_PROMPT
from .guidance import SYSTEM_GUIDENCE_TEMPLATE,SystemGuidenceKind,build_system_guidence_message


def build_system_prompt(*, role=AGENT_CORE_ROLE_PROMPT, tool_call_template=''):
    if not isinstance(role,str) or not role.strip():
        raise ValueError('角色提示词不能为空')
    return '\n\n'.join((role,build_agent_core_interaction_prompt(tool_call_template=tool_call_template),AGENT_CORE_WORKSPACE_PROMPT))

SYSTEM_PROMPT=build_system_prompt()

__all__=['AGENT_CORE_ROLE_PROMPT','AGENT_CORE_INTERACTION_PROMPT_TEMPLATE','build_agent_core_interaction_prompt',
    'AGENT_CORE_WORKSPACE_PROMPT','SYSTEM_GUIDENCE_TEMPLATE','SystemGuidenceKind','build_system_guidence_message',
    'build_system_prompt','SYSTEM_PROMPT']
