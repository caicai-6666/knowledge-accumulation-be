"""检索子 Agent 只组织条件，身份和祖先范围通过宿主闭包绑定。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskEndStatus = Literal['complete', 'cancelled', 'superseded', 'failed']


class RetrievalModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class MemoryRetrievalRequest(RetrievalModel):
    query: str = Field(strict=True, min_length=1, max_length=4096, description='希望找回的历史内容和明确限制。')
    reference_time: int = Field(strict=True, ge=0, description='当前任务创建时间，UTC Unix毫秒，用于相对日期解释。')


class FieldQuery(RetrievalModel):
    field: Literal['user_input', 'intermediate_output', 'final_output'] = Field(description='需要检索的公开内容字段；每个字段同时使用关键词和向量召回。')
    query: str = Field(strict=True, min_length=1, max_length=4096, description='对应字段的自然语言查询，保留对象、条件、否定和不确定性。')


class RetrievalPlan(RetrievalModel):
    queries: tuple[FieldQuery, ...] = Field(min_length=1, max_length=3, description='至少一个字段，每个字段最多设置一次。')
    created_from: int | None = Field(default=None, strict=True, ge=0, description='任务创建时间的包含下界，UTC毫秒；null不限。')
    created_before: int | None = Field(default=None, strict=True, ge=0, description='任务创建时间的不包含上界，UTC毫秒；null不限。')
    statuses: tuple[TaskEndStatus, ...] | None = Field(default=None, description='指定任务终态；null不限，不能重复或为空列表。')

    @model_validator(mode='after')
    def validate_plan(self):
        if len({q.field for q in self.queries}) != len(self.queries):
            raise ValueError('检索字段不能重复')
        if self.created_from is not None and self.created_before is not None and self.created_from >= self.created_before:
            raise ValueError('时间范围不合法')
        if self.statuses is not None and (not self.statuses or len(set(self.statuses)) != len(self.statuses)):
            raise ValueError('终态列表不合法')
        return self


class RankedTask(RetrievalModel):
    node_id: str = Field(pattern=r'^[0-9a-f]{32}$', description='历史任务的持久引用，查看页面时展示为任务 ID，供定位相邻任务使用。')
    score: float = Field(ge=0, allow_inf_nan=False, description='RRF排名分数，不表示答案置信度。')


class MemoryRetrievalResult(RetrievalModel):
    status: Literal['success','error']
    tasks: tuple[RankedTask, ...] = ()
    error_code: str | None = None
    error: str | None = None
