"""单节点多轮检索子图；身份范围只存在宿主回调，不进入模型状态。"""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from .schema import MemoryRetrievalRequest, MemoryRetrievalResult
from .agent import run_memory_retrieval


class State(TypedDict, total=False):
    request: MemoryRetrievalRequest
    result: MemoryRetrievalResult


def build_memory_retrieval_subgraph(**options):
    async def retrieve(state):
        return {'result':await run_memory_retrieval(state['request'], **options)}
    graph = StateGraph(State)
    graph.add_node('retrieve_memory', retrieve)
    graph.add_edge(START, 'retrieve_memory')
    graph.add_edge('retrieve_memory', END)
    return graph.compile()
