"""一次记忆检索的独立条件容器；六个方法通过检索子Agent工具适配调用。"""
from datetime import date as calendar_date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import Field, model_validator
from ...json_codec import ModelActionRejected

from .schema import (
    FieldQuery, MemoryRetrievalRequest, MemoryRetrievalResult, RetrievalModel,
    RetrievalPlan,
)

TaskEndStatus = Literal['complete', 'cancelled', 'superseded', 'failed']
_QUERY_FIELDS = ('user_input', 'intermediate_output', 'final_output')
_BEIJING = timezone(timedelta(hours=8))


class RetrievalConditions(RetrievalModel):
    """允许尚未选择字段的编辑状态；正式执行时再校验至少一个字段。"""
    created_from: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间下界，UTC Unix毫秒，包含边界；null表示不限。')
    created_before: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间上界，UTC Unix毫秒，不含边界；null表示不限。')
    statuses: tuple[TaskEndStatus, ...] | None = Field(default=None, description='限定的任务终态；null表示不限，不允许空列表或重复状态。')
    user_input: str | None = Field(default=None, min_length=1, description='用户输入字段的查询正文；null表示不启用该字段。')
    intermediate_output: str | None = Field(default=None, min_length=1, description='中途输出字段的查询正文；null表示不启用该字段。')
    final_output: str | None = Field(default=None, min_length=1, description='最终结论字段的查询正文；null表示不启用该字段。')

    @model_validator(mode='after')
    def validate_filters(self):
        if self.created_from is not None and self.created_before is not None and self.created_from >= self.created_before:
            raise ModelActionRejected('时间下界不小于上界，时间条件未更新。',
                '将 created_from 设为早于 created_before 的 UTC Unix 毫秒时间；不需要某个边界时传 null。')
        if self.statuses is not None and (not self.statuses or len(set(self.statuses)) != len(self.statuses)):
            raise ModelActionRejected('任务终态为空列表或存在重复项，状态条件未更新。',
                '传入至少一个且不重复的合法终态；清除状态限制时传 null。')
        return self


class MemoryRetrievalSession:
    """每次检索调用创建一个实例，不跨调用共享查询条件或执行结果。"""

    def __init__(self, request: MemoryRetrievalRequest | dict, *, search):
        self._request = MemoryRetrievalRequest.model_validate(request)
        self._search = search
        self._executing = False
        self._conditions = RetrievalConditions()
        self._result: MemoryRetrievalResult | None = None

    @property
    def request(self) -> MemoryRetrievalRequest:
        return self._request

    @property
    def conditions(self) -> RetrievalConditions:
        return self._conditions

    @property
    def result(self) -> MemoryRetrievalResult | None:
        """仅保留成功执行的正式结果；失败不作为终止标志。"""
        return self._result

    def _replace(self, **changes) -> RetrievalConditions:
        if self._executing:
            raise ModelActionRejected('查询正在执行，条件未修改。', '等待当前查询结束，不重复修改或执行同一查询。')
        if self._result is not None:
            raise ModelActionRejected('本次查询已经成功结束，条件未修改。', '使用本次查询结果；新的需求应另行发起检索。')
        # 完整校验候选副本后再提交，失败时原条件保持不变。
        candidate = RetrievalConditions.model_validate({**self._conditions.model_dump(), **changes})
        self._conditions = candidate
        return candidate

    def set_time_filter(
        self, *, date: str | None = None,
        created_from: int | None = None, created_before: int | None = None,
    ) -> RetrievalConditions:
        """设置单个北京时间自然日或UTC毫秒范围；全部为null时清除时间限制。"""
        if date is not None:
            if created_from is not None or created_before is not None:
                raise ValueError('单个日期和时间范围不能同时指定')
            try:
                day = calendar_date.fromisoformat(date)
                if day.isoformat() != date:
                    raise ValueError('日期格式不正确')
                start = datetime.combine(day, time.min, tzinfo=_BEIJING)
                end = start + timedelta(days=1)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('日期必须为有效的YYYY-MM-DD且可计算次日边界') from exc
            created_from = int(start.timestamp() * 1000)
            created_before = int(end.timestamp() * 1000)
        return self._replace(created_from=created_from, created_before=created_before)

    def set_status_filter(self, statuses: tuple[TaskEndStatus, ...] | None = None) -> RetrievalConditions:
        """替换任务终态限制；null清除限制，不能传入进行中等非终态。"""
        return self._replace(statuses=statuses)

    def set_user_input_query(self, query: str | None) -> RetrievalConditions:
        """设置用户输入查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(user_input=query)

    def set_intermediate_output_query(self, query: str | None) -> RetrievalConditions:
        """设置中途输出查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(intermediate_output=query)

    def set_final_output_query(self, query: str | None) -> RetrievalConditions:
        """设置最终结论查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(final_output=query)

    async def execute_query(self) -> MemoryRetrievalResult:
        """成功包括空结果；依赖失败不发布部分候选，也不改写已设置的条件。"""
        if self._result is not None:
            return self._result
        if self._executing:
            return MemoryRetrievalResult(status='error', error_code='query_busy',
                error='查询正在执行，尚未得到结果；请等待当前查询结束，不重复发起相同查询。')
        queries = tuple(FieldQuery(field=field, query=getattr(self._conditions, field))
                        for field in _QUERY_FIELDS if getattr(self._conditions, field) is not None)
        if not queries:
            return MemoryRetrievalResult(status='error', error_code='missing_query',
                error='尚未启用检索字段，本次未执行查询。请通过 set_user_input_query、'
                      'set_intermediate_output_query 或 set_final_output_query 设置至少一个非空查询，再调用 execute_query。')
        plan = RetrievalPlan(queries=queries, created_from=self._conditions.created_from,
            created_before=self._conditions.created_before, statuses=self._conditions.statuses)
        self._executing = True
        try:
            self._result = MemoryRetrievalResult(status='success', tasks=await self._search(plan))
            return self._result
        except Exception as error:
            import logging
            logging.getLogger(__name__).warning('记忆召回失败：error_type=%s', type(error).__name__)
            return MemoryRetrievalResult(status='error', error_code='query_execution_failed',
                error='记忆检索依赖暂时不可用，请稍后重试，不能据此判断没有相关记忆。')
        finally:
            self._executing = False
