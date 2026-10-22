"""助理专用工具组合点；当前不注册尚未实现的个人资料或日程能力。"""
from app.agent.core.tool import build_registry as build_core_registry


def build_registrations():
    return ()


def build_registry(*,additional=()):
    return build_core_registry(additional=(*build_registrations(),*additional))

__all__=['build_registrations','build_registry']
