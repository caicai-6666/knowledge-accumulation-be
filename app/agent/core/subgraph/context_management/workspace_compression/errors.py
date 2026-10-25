"""可确定未提交的工作区整理失败；依赖和提交故障不使用此类型。"""


class WorkspaceEditRejected(ValueError):
    """旧工作区仍为权威状态，主循环可提示模型有限次调整操作。"""
