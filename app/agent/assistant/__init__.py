"""个人助理装配入口；导入不读取配置、不建立外部连接。"""
from .factory import build_core,build_service
from .context import AssistantContext,ContextMaterial,build_context

__all__=['build_core','build_service','AssistantContext','ContextMaterial','build_context']
