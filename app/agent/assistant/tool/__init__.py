"""助理专用工具组合点；带身份的通信与提醒工具由消费宿主逐任务注入。"""
from app.agent.core.tool import build_registry as build_core_registry
from .finish_task import AssistantFinishTask


def build_registrations():
    return ()


def build_registry(*,additional=()):
    return build_core_registry(finish_task=AssistantFinishTask(),
                               additional=(*build_registrations(), *additional))

__all__=['build_registrations','build_registry']
