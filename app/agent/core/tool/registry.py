"""唯一工具注册契约：Schema、兼容解析和预演处理器共同绑定。"""
from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Callable
import re
from inspect import isawaitable
from pydantic import BaseModel
from ..json_codec import load_model_json,validate_model_payload
from ..workspace import WorkspacePayload
from .result import ToolExecution,ToolExecutionResult,ToolContentType,ToolPageReference
from .progress import ToolProgress,DEFAULT_TOOL_PROGRESS

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
            schema=entry.definition()['function']['parameters']
            if schema.get('type')!='object':raise ValueError('工具参数顶层必须为对象')
            check_descriptions(schema)
            self.entries[entry.name]=entry
        self.entries=MappingProxyType(self.entries)

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
            raise ValueError('工具未提供')
        entry=self.entries[name]
        arguments=(entry.parser(raw) if entry.parser is not None else
                   validate_model_payload(entry.arguments_model,load_model_json(raw)))
        if not isinstance(arguments,entry.arguments_model):
            raise TypeError('工具解析器返回类型与注册参数模型不一致')
        candidate=entry.handler(name,raw,workspace.model_copy(deep=True),arguments)
        candidate=WorkspacePayload.model_validate(candidate.model_dump())
        return arguments,candidate

    async def execute(self,name,arguments,*,publish_progress=None):
        entry=self.entries[name]
        progress=(entry.progress_factory(arguments.model_copy(deep=True))
                  if entry.progress_factory is not None else entry.progress)
        if not isinstance(progress,ToolProgress):
            raise TypeError('动态工具状态必须返回 ToolProgress')
        if publish_progress is not None:
            await publish_progress(progress)
            try:
                return await self._execute(name,arguments)
            finally:
                await publish_progress(DEFAULT_TOOL_PROGRESS)
        return await self._execute(name,arguments)

    async def _execute(self,name,arguments):
        """只在参数与状态验收后读取资料；结果提交前不能进入模型上下文。"""
        entry=self.entries[name]
        execution=ToolExecution(ToolExecutionResult(status='succeeded',tool_result={'status':'accepted'}))
        if entry.executor is not None:
            result=entry.executor(arguments.model_copy(deep=True))
            if isawaitable(result):
                result=await result
            # 不再把任意 JSON 包装成 accepted；执行后的契约错误由宿主按结果不确定停止。
            execution=(ToolExecution(result.result,result.after_commit) if isinstance(result,ToolExecution)
                       else ToolExecution(ToolExecutionResult.model_validate(result)))
        result=execution.result
        if result.status=='succeeded' and result.content.type not in entry.return_types:
            raise ValueError('工具实际返回类型不在注册声明中')
        return ToolExecution(result.model_copy(deep=True),execution.after_commit)


def build_registry(*, additional=()):
    # 各能力只负责自己的注册，组合入口不按工具名前缀猜测解析方式。
    from .workspace import build_workspace_registrations
    from .interaction import build_interaction_registrations
    from .context import build_context_registrations
    return ToolRegistry([*build_workspace_registrations(),*build_interaction_registrations(),
        *build_context_registrations(),*additional])
