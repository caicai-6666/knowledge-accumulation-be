"""探索模式上下文入口；当前使用已有树公开投影，不增加虚构业务字段。"""
from app.agent.core import ContextInjection


def build_context(base:ContextInjection) -> ContextInjection:
    return ContextInjection.model_validate(base.model_dump())

__all__=['build_context']
