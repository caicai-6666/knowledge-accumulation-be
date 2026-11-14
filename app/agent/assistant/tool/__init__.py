"""助理专用工具组合点；带身份的通信与提醒工具由消费宿主逐任务注入。"""
from app.agent.core.tool import build_registry as build_core_registry
from .finish_task import AssistantFinishTask


def build_registrations():
    return ()


def build_registry(*,additional=(),finish_hooks=(),user=None,task_type=None,validate_pruning=None):
    return build_core_registry(finish_task=AssistantFinishTask(user=user,hooks=finish_hooks,task_type=task_type,validate_pruning=validate_pruning),
                               additional=(*build_registrations(), *additional))

__all__=['build_registrations','build_registry']
