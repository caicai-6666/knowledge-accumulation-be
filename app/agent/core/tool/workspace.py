"""工作区工具参数、路径解析、无副作用编辑及注册。"""
from dataclasses import dataclass, replace
from textwrap import dedent
from typing import Annotated, Literal
from uuid import uuid4
from pydantic import BaseModel, Field, StringConstraints, model_validator
from ..json_codec import load_model_json,normalize_model_json,ModelActionRejected
from ..workspace import (WorkspaceObject,WorkspaceText,WorkspaceKey,WorkspacePayload,
    WorkspaceKnownInformation,WorkspaceRemainingDirection,WorkspaceExploredDirection)
from .hooks import ToolHookContext
from .result import OrdinaryToolContent, ToolExecution

WorkspacePath = Annotated[str, StringConstraints(strict=True, pattern=r'^/[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$')]


class ExplorationResult(WorkspaceObject):
    outcome: Literal['succeeded', 'failed', 'inconclusive'] = Field(description='本次探索成功、失败或尚无定论；未找到材料不能直接认定材料不存在。')
    conclusion: WorkspaceText = Field(description='本次已完成探索得到的精简结论、依据、限制或失败原因，不提交临时草稿。')
    information_ids: list[WorkspaceKey] = Field(default_factory=list, description='结论引用的已有已知信息 ID，不能重复或悬空；无引用时为空列表。')


class WorkspaceReplaceArguments(WorkspaceObject):
    path: WorkspacePath = Field(description='已有字段或条目的绝对路径，例如 /task_constraints/task、/task_constraints/supplements/supplement_001、/known_information/info_001/status；不允许根、区域容器、数组下标或通配符。')
    value: WorkspaceText | None | list[WorkspaceKey] | WorkspaceKnownInformation | WorkspaceRemainingDirection | WorkspaceExploredDirection = Field(
        description='完全替换目标位置的新值；类型与该位置一致。整体条目须提供完整对象；null 仅可用于 task 或 source；information_ids 用完整 ID 列表替换。未指定位置不变。')


class WorkspaceAddArguments(WorkspaceObject):
    path: Literal['/task_constraints/supplements', '/known_information', '/remaining_directions', '/explored_directions'] = Field(
        description='新增条目的目标容器，不能附加自拟 ID。用户补充传文本，已知信息或剩余方向传完整对象；已探索区传探索结果并指定 direction_id。')
    value: WorkspaceText | WorkspaceKnownInformation | WorkspaceRemainingDirection | ExplorationResult = Field(
        description='新增内容：补充文本、{content, source, status} 信息、{plan} 规划，或 {outcome, conclusion, information_ids} 探索结果；不携带条目 ID。')
    direction_id: WorkspaceKey | None = Field(default=None,
        description='仅向 /explored_directions 新增结果时必填，必须是剩余规划中已有的方向 ID；程序保留原规划和 ID 并原子移除剩余项。其他新增必须省略或为 null。')

    @model_validator(mode='after')
    def validate_target(self):
        types = {'/task_constraints/supplements': str, '/known_information': WorkspaceKnownInformation,
                 '/remaining_directions': WorkspaceRemainingDirection, '/explored_directions': ExplorationResult}
        if not isinstance(self.value, types[self.path]):
            raise ModelActionRejected('value 类型与新增目标容器不一致。', '根据 path 提供对应内容：补充传文本，已知信息传 content/source/status，剩余方向传 plan，探索结果传 outcome/conclusion/information_ids。')
        if (self.path == '/explored_directions') != (self.direction_id is not None):
            raise ModelActionRejected('direction_id 与新增容器不匹配。', '向 /explored_directions 新增时提供剩余方向的已有 ID；其他容器省略 direction_id 或设为 null。')
        return self


class WorkspaceDeleteArguments(WorkspaceObject):
    path: WorkspacePath = Field(description='要删除的已有完整条目路径，例如 /remaining_directions/direction_001；仅支持补充、已知信息、已探索或剩余方向条目，不删除根、容器和必填字段。不自动删除关联引用。')


@dataclass(frozen=True)
class WorkspaceEdit:
    payload: WorkspacePayload
    path: str
    removed_path: str | None = None


def parse_workspace_tool_arguments(name: str, raw_arguments: str) -> BaseModel:
    """只解析真实工具调用参数；拒绝重复键与非标准数值，不降级解析普通文本。"""
    if name not in _MODELS:
        raise ModelActionRejected('该工具不是工作区编辑工具。', '仅使用 workspace_add、workspace_replace 或 workspace_delete 编辑工作区。')

    decoded = load_model_json(raw_arguments)
    schema = _MODELS[name].model_json_schema()
    # value 是文本/对象的联合类型，必须由目标路径消歧，不能按字符串外观猜测。
    if isinstance(decoded, dict) and isinstance(decoded.get('path'), str):
        path = decoded['path']
        target = None
        if name == 'workspace_add':
            target = {'/known_information': WorkspaceKnownInformation,
                      '/remaining_directions': WorkspaceRemainingDirection,
                      '/explored_directions': ExplorationResult}.get(path)
        elif name == 'workspace_replace':
            parts = path.split('/')[1:]
            if len(parts) == 2:
                target = {'known_information': WorkspaceKnownInformation,
                          'remaining_directions': WorkspaceRemainingDirection,
                          'explored_directions': WorkspaceExploredDirection}.get(parts[0])
            elif len(parts) == 3 and parts[0] == 'explored_directions' and parts[2] == 'information_ids':
                schema['properties']['value'] = {'type': 'array', 'items': {'type': 'string'}}
        if target is not None:
            target_schema = target.model_json_schema()
            schema.setdefault('$defs', {}).update(target_schema.pop('$defs', {}))
            schema['properties']['value'] = target_schema
    return _MODELS[name].model_validate(normalize_model_json(decoded, schema))



_ENTRY_REGIONS = ('known_information', 'explored_directions', 'remaining_directions')


def _is_entry(parts: list[str]) -> bool:
    return (len(parts) == 2 and parts[0] in _ENTRY_REGIONS) or (
        len(parts) == 3 and parts[:2] == ['task_constraints', 'supplements'])


def _locate(payload: dict, parts: list[str]):
    current = payload
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise ModelActionRejected('path 指向的位置在最新工作区中不存在。', '从最新工作区复制已有路径；新增条目使用 workspace_add 指向容器，不自拟条目 ID。')
        current = current[part]
    return current


def prepare_workspace_edit(snapshot: WorkspacePayload, name: str, raw_arguments: str) -> WorkspaceEdit:
    """在深拷贝中应用一次操作，再校验完整工作区；失败不修改原快照。"""
    arguments = parse_workspace_tool_arguments(name, raw_arguments)
    payload = snapshot.model_dump()
    parts = arguments.path.split('/')[1:]
    removed_path = None
    path = arguments.path
    if isinstance(arguments, WorkspaceAddArguments):
        value = arguments.model_dump()['value']
        if arguments.path == '/explored_directions':
            key = arguments.direction_id
            if key not in payload['remaining_directions']:
                raise ModelActionRejected('direction_id 不属于当前剩余规划。', '从最新 remaining_directions 选择已有方向 ID；已经收束的方向不要再次收束。')
            direction = payload['remaining_directions'].pop(key)
            payload['explored_directions'][key] = {'plan': direction['plan'], **value}
            removed_path = f'/remaining_directions/{key}'
        else:
            target = _locate(payload, parts)
            prefix = {'/task_constraints/supplements': 'supplement', '/known_information': 'info',
                      '/remaining_directions': 'direction'}[arguments.path]
            key = f'{prefix}_{uuid4().hex}'
            while key in target or key in payload['explored_directions']:
                key = f'{prefix}_{uuid4().hex}'
            target[key] = value
        path += f'/{key}'
    else:
        entry = _is_entry(parts)
        if isinstance(arguments, WorkspaceReplaceArguments):
            allowed = entry or parts == ['task_constraints', 'task'] or (
                len(parts) == 3 and parts[0] in _ENTRY_REGIONS)
            if not allowed:
                raise ModelActionRejected('path 不是允许替换的字段或完整条目。', '替换已有字段或条目，例如 /task_constraints/task 或 /known_information/已有ID；不要替换根、区域容器或数组下标。')
        elif not entry:
            raise ModelActionRejected('path 不是允许删除的完整条目。', '只删除补充、已知信息、已探索或剩余方向的已有完整条目；不要删除根、容器或必填字段。')
        parent = _locate(payload, parts[:-1])
        if parts[-1] not in parent:
            raise ModelActionRejected('path 指向的位置在最新工作区中不存在。', '从最新工作区复制已有路径；新增条目使用 workspace_add 指向容器，不自拟条目 ID。')
        if isinstance(arguments, WorkspaceReplaceArguments):
            parent[parts[-1]] = arguments.model_dump()['value']
        else:
            del parent[parts[-1]]
            removed_path = path
    validated = WorkspacePayload.model_validate(payload)
    return WorkspaceEdit(payload=validated, path=path, removed_path=removed_path)


_MODELS = {
    'workspace_add': WorkspaceAddArguments,
    'workspace_replace': WorkspaceReplaceArguments,
    'workspace_delete': WorkspaceDeleteArguments,
}


def render_workspace_feedback(
    arguments: BaseModel,
    execution: ToolExecution,
    context: ToolHookContext,
) -> ToolExecution:
    # 参数及引用拒绝发生在预演阶段，由原校验分支反馈；此处不重做编辑或分配 ID。
    if execution.result.status != 'succeeded':
        return execution

    if isinstance(arguments, WorkspaceAddArguments):
        title, label = '工作区新增', '目标容器'
    elif isinstance(arguments, WorkspaceReplaceArguments):
        title, label = '工作区替换', '目标路径'
    else:
        title, label = '工作区删除', '目标路径'

    # 独立调用注册表并不意味着已提交工作区，不能冒充完整宿主的成功反馈。
    feedback = context.workspace_feedback or '状态：编辑已通过预演，等待宿主提交。'
    text = dedent(f"""
        ### {title}

        {feedback}
        {label}：{arguments.path}
    """).strip()
    if isinstance(arguments, WorkspaceAddArguments) and arguments.direction_id is not None:
        text += f'\n探索方向 ID：{arguments.direction_id}'

    return replace(
        execution,
        result=execution.result.model_copy(
            update={'content': OrdinaryToolContent(text=text)},
        ),
    )


def build_workspace_registrations():
    from functools import partial

    from .registry import RegisteredTool

    descriptions = {
        'workspace_add': (
            '当你需要保存后续步骤仍会使用的重要信息、任务补充、探索计划或探索结果时，使用这个工具。'
            '向指定工作区容器新增一个条目，新增位置以最新工作区为准。'
            '普通新增由程序生成 ID；新增已探索结果时必须指定剩余方向 ID，原子转移并保留原规划。'
        ),
        'workspace_replace': (
            '当你需要更新工作区中已有的信息、修正任务约束或整理已有条目时，使用这个工具。'
            '用新值替换已有字段或完整条目，保留稳定 ID 和其他内容；路径不存在、类型或引用不合法时整次拒绝。'
            '用户要求以实际陈述为准，不用自己的规划代替。'
        ),
        'workspace_delete': (
            '当你需要清理工作区中过时、重复或已无保留价值的条目，为后续信息腾出空间时，使用这个工具。'
            '删除指定条目，不级联删除其他信息；必要字段、容器、缺失条目或仍被引用的信息不能删除。'
        ),
    }

    def prepare(name, raw, workspace, arguments):
        return prepare_workspace_edit(workspace, name, raw).payload

    return [
        RegisteredTool(
            name,
            descriptions[name],
            model,
            prepare,
            partial(parse_workspace_tool_arguments, name),
            hooks=(render_workspace_feedback,),
        )
        for name, model in _MODELS.items()
    ]
