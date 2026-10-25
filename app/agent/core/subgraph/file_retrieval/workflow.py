"""独立检索子图；身份和权限不进入模型状态。"""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from .agent import run_retrieval


class State(TypedDict, total=False):
    query: str
    result: tuple


def build_file_retrieval_subgraph(**options):
    async def retrieve(state):
        return {'result':await run_retrieval(state['query'],**options)}
    graph=StateGraph(State)
    graph.add_node('retrieve_files',retrieve)
    graph.add_edge(START,'retrieve_files')
    graph.add_edge('retrieve_files',END)
    return graph.compile()
