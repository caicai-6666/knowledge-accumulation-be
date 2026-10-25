"""探索模式的提示词、工具和上下文组合；不建立第二套循环。"""
from app.agent.core import AgentCore
from app.config.agent import load_agent_core_settings
from app.service.question_agent import QuestionAgentService
from .context import build_context
from .prompt import build_system_prompt
from .tool import build_registry
from .progress_reminder import ExplorationGuidance
from .output_stream import InteractionOutputStream


def build_core(settings,*,client=None,counter=None,options=None,tools=()):
    options=options if options is not None else load_agent_core_settings()
    return AgentCore(settings,client=client,counter=counter,options=options,
        registry=build_registry(additional=tools),prompt_builder=build_system_prompt,guidance_factory=ExplorationGuidance)


def build_service(nodes,settings,*,client=None,counter=None,options=None,tools=(),tool_loader=None,file_service=None,file_cache=None,expert_settings=None,expert_client=None,web_search_service=None,web_graph_factory=None,memory_backend=None,platform_directory=None,file_retrieval=None,registry_files=None,registry_file_access=None):
    return QuestionAgentService(nodes,build_core(settings,client=client,counter=counter,options=options,tools=tools),
        context_builder=lambda user,base:build_context(base),expected_tree_kind='exploration',tool_loader=tool_loader,output_observer_factory=InteractionOutputStream,
        file_service=file_service,file_cache=file_cache,expert_settings=expert_settings,expert_client=expert_client,
        web_search_service=web_search_service,web_graph_factory=web_graph_factory,memory_backend=memory_backend,platform_directory=platform_directory,file_retrieval=file_retrieval,registry_files=registry_files,registry_file_access=registry_file_access)

__all__=['build_core','build_service']
