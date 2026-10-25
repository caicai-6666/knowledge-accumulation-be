"""探索最终答复：提交最终正文，由探索宿主通过 SSE 交付。"""
from app.agent.core.tool.base import FinishTaskTool


class ExplorationFinishTask(FinishTaskTool):
    description = '当你需要提交最终答复，或因缺少必要信息而需要用户补充后才能继续时，使用这个工具。向用户提交最终反馈并正式结束本轮任务；可交付答案、说明限制或请求必要信息，成功后不再继续本轮调用。'
