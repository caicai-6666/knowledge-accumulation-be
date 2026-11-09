"""助理装配；用户资料每次按可信身份加载，不在共享 Core 上缓存。"""
from inspect import isawaitable
from app.agent.core import AgentCore
from app.config.agent import load_agent_core_settings
from app.service.question_agent import QuestionAgentService
from .context import AssistantContext,build_context
from .prompt import build_system_prompt
from .tool import build_registry
from .tool.finish_task import AssistantFinishTask
from .tool.image_authorization import wrap_image_authorize
from app.agent.core.tool import ToolRegistry


def build_core(settings,*,client=None,counter=None,options=None,tools=(),prompt_builder=None,finish_hooks=(),user=None):
    options=options if options is not None else load_agent_core_settings()
    return AgentCore(settings,client=client,counter=counter,options=options,
        registry=build_registry(additional=tools,finish_hooks=finish_hooks,user=user),prompt_builder=prompt_builder or build_system_prompt)


def build_service(nodes,settings,*,client=None,counter=None,options=None,tools=(),context_loader=None,tool_loader=None,file_service=None,file_cache=None,expert_settings=None,expert_client=None,web_search_service=None,web_graph_factory=None,memory_backend=None,platform_directory=None,file_retrieval=None,registry_files=None,registry_file_access=None,prompt_builder=None,platform_a2a=None,finish_hooks=(),file_visibility=None,file_delivery=None):
    def bind_registry(user, registry, tree_id):
        # 复用本轮可信员工快照；保留转换钩子及其他工具，不修改共享注册表。
        final = AssistantFinishTask(user=user, hooks=finish_hooks,
            validate_files=(lambda ids: file_delivery.validate(user.userid, tree_id, ids)) if file_delivery is not None else None).build_registration()
        return ToolRegistry([final if name == 'finish_task' else entry
                             for name, entry in registry.entries.items()])

    async def enrich(user,base):
        personal=None
        if context_loader is not None:
            value=context_loader(user)
            value=await value if isawaitable(value) else value
            personal=AssistantContext.model_validate(value.model_dump() if isinstance(value,AssistantContext) else value)
        return build_context(base,personal)
    return QuestionAgentService(nodes,build_core(settings,client=client,counter=counter,options=options,tools=tools,prompt_builder=prompt_builder,finish_hooks=finish_hooks),
        file_authorize_wrapper=lambda authorize, user: wrap_image_authorize(
            authorize, userid=user.userid, visibility=file_visibility),
        context_builder=enrich,registry_builder=bind_registry,expected_tree_kind='assistant',tool_loader=tool_loader,file_service=file_service,file_cache=file_cache,expert_settings=expert_settings,expert_client=expert_client,
        web_search_service=web_search_service,web_graph_factory=web_graph_factory,memory_backend=memory_backend,platform_directory=platform_directory,file_retrieval=file_retrieval,registry_files=registry_files,registry_file_access=registry_file_access,platform_a2a=platform_a2a)

__all__=['build_core','build_service']
