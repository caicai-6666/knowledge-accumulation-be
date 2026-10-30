"""唯一工具注册契约：Schema、兼容解析和预演处理器共同绑定。"""
from dataclasses import dataclass, replace
from typing import Literal
from types import MappingProxyType
from collections.abc import Callable
import re
from inspect import isawaitable
from contextvars import ContextVar
from pydantic import BaseModel,ValidationError
from ..json_codec import load_model_json,validate_model_payload,ModelActionRejected,model_validation_feedback
from ..workspace import WorkspacePayload
from .result import ToolExecution,ToolExecutionResult,ToolContentType,ToolPageReference
from .progress import ToolProgress,DEFAULT_TOOL_PROGRESS
from .hooks import ToolHook,ToolHookContext

# 可信工具调用标识仅供外部操作幂等使用，不进入 Schema、工具结果或提示词。
current_tool_call_id = ContextVar('current_tool_call_id', default=None)

@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable
    parser: Callable[[str],BaseModel] | None = None
    executor: Callable | None = None
    return_types: tuple[ToolContentType,...] = ('ordinary',)
    page_resolver: Callable | None = None
    # 所有工具注册时均声明动作类型；基础工具默认思考，专用工具显式覆盖。
    progress: ToolProgress = DEFAULT_TOOL_PROGRESS
    progress_factory: Callable | None = None
    # 公共输出提交与流式预览读取此声明，不按模式或工具名猜测行为。
    output_kind: Literal["intermediate", "final"] | None = None
    # 输出仍须提交，只有通过此条件才交给宿主发送；条件不进入模型 Schema。
    deliver_output: Callable | None = None
    hooks: tuple[ToolHook, ...] = ()

    def __post_init__(self):
        # 注册时复制容器，外部列表追加不会影响本工具，更不会影响其他工具。
        object.__setattr__(self, 'hooks', tuple(self.hooks))
        if any(not callable(hook) for hook in self.hooks):
            raise TypeError('工具钩子必须可调用')

    def definition(self):
        return {'type':'function','function':{'name':self.name,'description':self.description,
            'parameters':self.arguments_model.model_json_schema(),'strict':False}}


def check_descriptions(schema):
    if isinstance(schema,dict):
        for field in schema.get('properties',{}).values():
            if not isinstance(field.get('description'),str) or not field['description'].strip():
                raise ValueError('工具参数缺少字段说明')
        extra=schema.get('additionalProperties')
        if isinstance(extra,dict) and extra and not extra.get('description'):
            raise ValueError('动态属性缺少说明')
        for child in schema.values():
            check_descriptions(child)
    elif isinstance(schema,list):
        for child in schema:
            check_descriptions(child)


class ToolRegistry:
    def __init__(self,entries):
        self.entries={}
        for entry in entries:
            if not isinstance(entry,RegisteredTool):
                raise TypeError('工具必须使用 RegisteredTool 注册')
            if not isinstance(entry.name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',entry.name):
                raise ValueError('工具名称不合法')
            if not isinstance(entry.arguments_model,type) or not issubclass(entry.arguments_model,BaseModel):
                raise TypeError('工具参数必须使用 Pydantic 模型')
            if entry.name in self.entries or not entry.description.strip() or not callable(entry.handler):
                raise ValueError('工具注册重复或不合法')
            if entry.parser is not None and not callable(entry.parser):
                raise TypeError('工具解析器必须可调用')
            if entry.executor is not None and not callable(entry.executor):
                raise TypeError('工具执行器必须可调用')
            if (not isinstance(entry.return_types,tuple) or not entry.return_types
                    or any(kind not in ('ordinary','foldable') for kind in entry.return_types)
                    or len(set(entry.return_types))!=len(entry.return_types)):
                raise ValueError('工具返回类型必须为非空、不重复的 ordinary/foldable 元组')
            if entry.page_resolver is not None and not callable(entry.page_resolver):
                raise TypeError('页面解析器必须可调用')
            if not isinstance(entry.progress,ToolProgress):
                raise TypeError('工具状态必须使用 ToolProgress')
            if entry.progress_factory is not None and not callable(entry.progress_factory):
                raise TypeError('动态工具状态必须可调用')
            if 'foldable' in entry.return_types and (entry.executor is None or entry.page_resolver is None):
                raise ValueError('可折叠工具必须绑定执行器和页面解析器')
            if entry.output_kind not in (None, 'intermediate', 'final'):
                raise ValueError('不支持的公开输出类别')
            if entry.deliver_output is not None and (entry.output_kind is None or not callable(entry.deliver_output)):
                raise ValueError('条件交付只适用于输出工具，且必须可调用')
            schema=entry.definition()['function']['parameters']
            if entry.output_kind is not None:
                content=schema.get('properties',{}).get('content',{})
                if content.get('type')!='string' or 'content' not in schema.get('required',[]):
                    raise ValueError('公开输出工具必须声明必填字符串 content')
            if schema.get('type')!='object':raise ValueError('工具参数顶层必须为对象')
            check_descriptions(schema)
            self.entries[entry.name]=entry
        self.entries=MappingProxyType(self.entries)

    def require_mode_tools(self):
        """主循环必须具有模式最终输出；独立子工具集合仍可只构造 ToolRegistry。"""
        from .base import OutputArguments
        final = self.entries.get('finish_task')
        if final is None or final.output_kind != 'final' or not issubclass(final.arguments_model, OutputArguments):
            raise ValueError('必须提供满足契约的模式 finish_task 实现')

    def output_kinds(self):
        return {name:entry.output_kind for name,entry in self.entries.items() if entry.output_kind is not None}

    def definitions(self):
        return [entry.definition() for entry in self.entries.values()]

    def require_supported_content(self,supported=('ordinary',)):
        """宿主在模型调用、工具执行及状态提交前验收整批工具的内容能力。"""
        for entry in self.entries.values():
            if not set(entry.return_types)<=set(supported):
                raise ValueError('宿主不支持工具声明的返回类型')

    async def resolve_page(self,name,reference):
        entry=self.entries[name]
        if 'foldable' not in entry.return_types or entry.page_resolver is None:
            raise ValueError('工具没有页面解析能力')
        reference=ToolPageReference.model_validate(reference).model_copy(deep=True)
        value=entry.page_resolver(reference)
        return await value if isawaitable(value) else value

    def prepare(self,name,raw,workspace):
        if name not in self.entries:
            raise ModelActionRejected('请求的工具不在当前可用工具列表中，动作未执行。',
                '从本轮实际提供的工具中选择名称并按其 Schema 调用；缺少能力时说明限制，不模拟调用成功。')
        entry=self.entries[name]
        try:
            arguments=(entry.parser(raw) if entry.parser is not None else
                       validate_model_payload(entry.arguments_model,load_model_json(raw)))
        except ValidationError as error:
            raise model_validation_feedback(error,entry.arguments_model) from None
        if not isinstance(arguments,entry.arguments_model):
            raise TypeError('工具解析器返回类型与注册参数模型不一致')
        try:
            candidate=entry.handler(name,raw,workspace.model_copy(deep=True),arguments)
        except ValidationError as error:
            raise model_validation_feedback(error,WorkspacePayload) from None
        candidate=WorkspacePayload.model_validate(candidate.model_dump())
        return arguments,candidate

    async def execute(self,name,arguments,*,publish_progress=None,call_id=None,hook_context=None):
        token = current_tool_call_id.set(call_id)
        try:
            return await self._execute_with_progress(name, arguments, publish_progress=publish_progress,
                hook_context=hook_context if hook_context is not None else ToolHookContext())
        finally:
            current_tool_call_id.reset(token)

    async def _execute_with_progress(self,name,arguments,*,publish_progress=None,hook_context):
        entry=self.entries[name]
        progress=(entry.progress_factory(arguments.model_copy(deep=True))
                  if entry.progress_factory is not None else entry.progress)
        # 文件名称等动作资料须先异步鉴权读取；同步状态工厂继续按原契约执行。
        if isawaitable(progress):
            progress=await progress
        if not isinstance(progress,ToolProgress):
            raise TypeError('动态工具状态必须返回 ToolProgress')
        if publish_progress is not None:
            await publish_progress(progress)
            try:
                return await self._execute(name,arguments,hook_context)
            finally:
                await publish_progress(DEFAULT_TOOL_PROGRESS)
        return await self._execute(name,arguments,hook_context)

    async def _execute(self,name,arguments,hook_context):
        """只在参数与状态验收后读取资料；结果提交前不能进入模型上下文。"""
        entry=self.entries[name]
        execution=ToolExecution(ToolExecutionResult(status='succeeded',tool_result={'status':'accepted'}))
        if entry.executor is not None:
            result=entry.executor(arguments.model_copy(deep=True))
            if isawaitable(result):
                result=await result
            # 不再把任意 JSON 包装成 accepted；执行后的契约错误由宿主按结果不确定停止。
            execution=(replace(result) if isinstance(result,ToolExecution)
                       else ToolExecution(ToolExecutionResult.model_validate(result)))
        if entry.output_kind is not None and execution.result.status=='succeeded' and execution.output is None:
            execution=replace(execution,output=arguments.content)
        execution=self._validate_execution(entry,execution)
        # 仅运行本注册项的钩子，逐个等待；失败/未知回执也可加工，但不能改写执行事实。
        for hook in entry.hooks:
            previous=execution
            value=hook(arguments.model_copy(deep=True),replace(execution,
                result=execution.result.model_copy(deep=True)),hook_context)
            value=await value if isawaitable(value) else value
            execution=self._validate_execution(entry,value)
            if execution.result.status!=previous.result.status:
                raise ValueError('工具钩子不能改写执行状态')
            if execution.after_commit is not previous.after_commit:
                raise ValueError('工具钩子不能替换或丢弃提交回调')
        return execution

    @staticmethod
    def _validate_execution(entry,execution):
        if not isinstance(execution,ToolExecution):
            raise TypeError('工具钩子必须返回 ToolExecution')
        execution=replace(execution)
        result=execution.result
        if result.status=='succeeded' and result.content.type not in entry.return_types:
            raise ValueError('工具实际返回类型不在注册声明中')
        if execution.output is not None and entry.output_kind is None:
            raise ValueError('只有输出工具可以返回输出正文')
        if entry.output_kind is not None and result.status=='succeeded' and execution.output is None:
            raise ValueError('输出工具不能丢弃输出正文')
        return replace(execution,result=result.model_copy(deep=True))


def build_builtin_registrations():
    """可直接复用的基础工具；不含必须由模式实现的最终输出。"""
    from .workspace import build_workspace_registrations
    from .context import build_context_registrations
    from .date_calculator import build_date_calculation_registration
    from .current_time import build_current_time_registration
    return [build_date_calculation_registration(), build_current_time_registration(),
            *build_workspace_registrations(), *build_context_registrations()]


def build_registry(*, finish_task, additional=()):
    from .base import FinishTaskTool
    if not isinstance(finish_task, FinishTaskTool):
        raise TypeError('必须显式提供继承 FinishTaskTool 的模式实现')
    registry = ToolRegistry([*build_builtin_registrations(), finish_task.build_registration(), *additional])
    registry.require_mode_tools()
    return registry
