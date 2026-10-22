"""复杂任务探索装配入口；与助理共用 Core 和树持久化。"""
from .factory import build_core,build_service
from .context import build_context

__all__=['build_core','build_service','build_context']
