"""工作区副本整理与主题摘要组合；全部通过后交给宿主提交。"""
from ...context_budget import validate_count
from ...context_rendering.workspace import render_workspace_section
from .schema import CompressionArguments
from .topic_summary.workflow import summarize
from .workspace_compression import compress_workspace


async def compress(*,source,workspace,focus,generate,counter,generation,summary_generation,context_window,
                   max_errors,workspace_budget,summary_limit,protected=None,tool_call_template=''):
    refined=workspace.model_copy(deep=True)
    wc=validate_count(await counter([{'role':'user','content':render_workspace_section(workspace)}],[],generation))
    if wc>=workspace_budget:
        refined=await compress_workspace(workspace=workspace,generate=generate,counter=counter,
            generation=generation,context_window=context_window,max_errors=max_errors,
            token_budget=workspace_budget,protected=protected,tool_call_template=tool_call_template)
    summary=await summarize(source=source,focus=focus,generate=generate,counter=counter,
        generation=summary_generation,
        context_window=context_window,max_errors=max_errors,summary_limit=summary_limit,
        tool_call_template=tool_call_template)
    return CompressionArguments(summary=summary,workspace=refined)
