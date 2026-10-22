"""上下文装配入口；具体渲染在 context_rendering，预算在 context_budget。"""
from .context_rendering import build_messages,build_fixed_messages,render_history,render_workspace_section

__all__=['build_messages','build_fixed_messages','render_history','render_workspace_section']
