"""个人助理装配入口；导入不读取配置、不建立外部连接。"""
from .context import AssistantContext, ContextMaterial, build_context
from .factory import (
    ASSISTANT_BUILTIN_TOOLS,
    ASSISTANT_OUTPUT_GUIDANCE,
    build_assistant_prompt_options,
    build_assistant_role_prompt,
    build_assistant_tools,
)

__all__ = [
    'ASSISTANT_BUILTIN_TOOLS',
    'ASSISTANT_OUTPUT_GUIDANCE',
    'AssistantContext',
    'ContextMaterial',
    'build_assistant_prompt_options',
    'build_assistant_role_prompt',
    'build_assistant_tools',
    'build_context',
]
