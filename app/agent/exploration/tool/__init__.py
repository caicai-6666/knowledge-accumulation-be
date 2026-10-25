"""探索专用工具组合点；基本运转工具只在 Core 定义一份。"""
from app.agent.core.tool import build_registry as build_core_registry
from .emit_progress import build_progress_registration
from .finish_task import ExplorationFinishTask


def build_registrations():
    return (build_progress_registration(),)


def build_registry(*,additional=()):
    return build_core_registry(finish_task=ExplorationFinishTask(),additional=(*build_registrations(),*additional))

__all__=['build_registrations','build_registry']
