"""个人助理提示词自包含入口；不依赖 app.agent.core。"""
from __future__ import annotations

from agent_core import PromptOptions
from agent_core.core.prompt import build_system_prompt as _compose_system_prompt

from app.agent.exploration.tools.prompt import (
    _COMPANY_BACKGROUND_PROMPT,
    _PLATFORM_COLLABORATION_PROMPT,
)

from .personal_information import ASSISTANT_PERSONAL_INFORMATION_PROMPT
from .portrait import ASSISTANT_PORTRAIT_PROMPT
from .role import ASSISTANT_ROLE_PROMPT

ASSISTANT_OUTPUT_GUIDANCE = (
    '### 个人助理输出与交付规则\n\n'
    '个人助理的 finish_task 必须明确填写 send_to_user。true 将最终正文发送给所属用户，'
    'false 仅保存结论并结束本轮；接收用户由程序绑定。依据本轮所属用户信息中的在职状态，'
    '所属员工已离职时必须选择 send_to_user=false，仅保存结论，此规则优先于答复、提醒、'
    '澄清和忠实条款中的通知要求。提醒消息先交给助理判断，不会自动通知用户。需要向其他助理沟通时，'
    '仅使用实际提供的 send_assistant_message，不以 finish_task 代替；目标员工已离职也可联系其助理。'
    '比如内部整理完成且无须通知时，填写 content="已完成整理"、send_to_user=false；'
    '所属员工在职且需要答复时填写完整正文并选择 true。'
)


def build_assistant_role_prompt() -> str:
    """组装个人助理完整角色提示词（供 agent-core `AgentRuntime.create/restore(role=...)` 使用）。"""
    return '\n\n'.join(
        (
            ASSISTANT_ROLE_PROMPT,
            _COMPANY_BACKGROUND_PROMPT,
            _PLATFORM_COLLABORATION_PROMPT,
            ASSISTANT_PERSONAL_INFORMATION_PROMPT,
            ASSISTANT_PORTRAIT_PROMPT,
        )
    )


def build_assistant_prompt_options() -> PromptOptions:
    """构造个人助理系统提示词装配选项，将输出与交付规则注入「用户输出与任务收敛」章节。"""
    return PromptOptions(output_guidance=ASSISTANT_OUTPUT_GUIDANCE)


def build_system_prompt(*, tool_call_template: str = '') -> str:
    """兼容入口：基于 agent-core 组合完整系统提示词。"""
    return _compose_system_prompt(
        role=build_assistant_role_prompt(),
        tool_call_template=tool_call_template,
        output_guidance=ASSISTANT_OUTPUT_GUIDANCE,
    )


__all__ = [
    'ASSISTANT_OUTPUT_GUIDANCE',
    'ASSISTANT_PERSONAL_INFORMATION_PROMPT',
    'ASSISTANT_PORTRAIT_PROMPT',
    'ASSISTANT_ROLE_PROMPT',
    'build_assistant_prompt_options',
    'build_assistant_role_prompt',
    'build_system_prompt',
]
