"""通用四区域工作区；借鉴合同项目结构，不迁入合同能力或旧格式兼容。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

WorkspaceText = Annotated[str, StringConstraints(strict=True, pattern=r"\S")]
WorkspaceKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]

class WorkspaceObject(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class WorkspaceTaskConstraints(WorkspaceObject):
    task: WorkspaceText | None = Field(description='用户当前明确提出的任务；尚未确定时为 null，不从旧规划猜测用户要求。')
    supplements: dict[WorkspaceKey, Annotated[WorkspaceText, Field(description='该稳定业务键对应的用户补充要求。')]] = Field(description='按稳定补充 ID 保存的用户立场、范围、条件和交付要求；不记录助手自己的计划。')

    @model_validator(mode='after')
    def validate_supplement_keys(self):
        # 局部更新接口以 task 定位主任务，补充键不能占用该保留名。
        if 'task' in self.supplements:
            raise ValueError('用户补充不能使用保留键 task')
        return self


class WorkspaceKnownInformation(WorkspaceObject):
    content: WorkspaceText = Field(description='可复用的事实、背景或材料线索，保留对象、适用范围和必要限制。')
    source: WorkspaceText | None = Field(description='实际用户陈述、文件及页码、外部来源或分析依据；没有可靠来源时为 null，不猜测。')
    status: Literal['confirmed', 'user_reported', 'unverified', 'conflicted'] = Field(
        description='信息状态：已确认、用户陈述、待核验或存在冲突；用户陈述不自动成为客观事实。')

    @model_validator(mode='after')
    def validate_source(self):
        if self.status == 'confirmed' and self.source is None:
            raise ValueError('已确认信息必须保留来源')
        return self


class WorkspaceRemainingDirection(WorkspaceObject):
    plan: WorkspaceText = Field(description='尚需尝试的探索方向，明确目的、对象和必要前提；不是单次调用流水账。')


class WorkspaceExploredDirection(WorkspaceObject):
    plan: WorkspaceText = Field(description='已经实际尝试的规划及其范围，保留探索目的。')
    outcome: Literal['succeeded', 'failed', 'inconclusive'] = Field(description='探索结果：成功、失败或尚无定论；未找到材料不自动意味着失败或材料不存在。')
    conclusion: WorkspaceText = Field(description='本次探索得到的结论、依据、限制或失败原因，不记录冗长草稿和临时纠错链。')
    information_ids: list[WorkspaceKey] = Field(default_factory=list, description='本次结论引用的已有已知信息 ID；没有引用时为空，不得重复或悬空。')


class WorkspacePayload(WorkspaceObject):
    task_constraints: WorkspaceTaskConstraints = Field(description='用户的当前任务及后续补充要求。')
    known_information: dict[WorkspaceKey, Annotated[WorkspaceKnownInformation, Field(description='该稳定信息键对应的一条事实、来源与确认状态。')]] = Field(description='以稳定信息 ID 定位的可复用事实、背景和材料线索。')
    explored_directions: dict[WorkspaceKey, Annotated[WorkspaceExploredDirection, Field(description='该稳定方向键对应的已尝试计划、结果、结论及信息引用。')]] = Field(description='以稳定方向 ID 定位的已尝试规划、结果及结论。')
    remaining_directions: dict[WorkspaceKey, Annotated[WorkspaceRemainingDirection, Field(description='该稳定方向键对应的尚需尝试的探索计划。')]] = Field(description='以稳定方向 ID 定位的剩余规划；执行中的方向在形成结果前仍保留于此。')

    @model_validator(mode='after')
    def validate_references(self):
        if self.explored_directions.keys() & self.remaining_directions.keys():
            raise ValueError('同一方向 ID 不能同时存在于已探索与剩余规划中')
        for key, direction in self.explored_directions.items():
            ids = direction.information_ids
            if len(ids) != len(set(ids)) or any(ref not in self.known_information for ref in ids):
                raise ValueError(f'explored_directions.{key}.information_ids 含重复或不存在的信息引用')
        return self


def empty_workspace() -> WorkspacePayload:
    return WorkspacePayload(task_constraints=WorkspaceTaskConstraints(task=None,supplements={}),
        known_information={},explored_directions={},remaining_directions={})

def read_workspace(value: dict) -> WorkspacePayload:
    # 仅空快照兼容；不猜测已有任意 JSON 或旧合同专属格式。
    return WorkspacePayload.model_validate(value) if value else empty_workspace()
