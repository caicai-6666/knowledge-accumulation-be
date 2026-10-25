"""主题规划、两区域工作记忆及落盘格式；来源标签只在本次整理中有效。"""
from typing import Literal
from pydantic import Field, model_validator
from ....workspace import WorkspaceObject, WorkspaceText
from ....tool import check_descriptions


class SummaryTopic(WorkspaceObject):
    topic_id: WorkspaceText = Field(description='主题唯一标识；同标识再次提交完整替换该主题，保持原顺序。')
    title: WorkspaceText = Field(description='有助于后续工作的主题名称，可跨多个任务，不按任务数量拆分。')
    scope: WorkspaceText = Field(description='一至三句话的提取范围、关键限制及排除范围；不写事实清单、完整摘要或新增待办。')
    task_ids: list[WorkspaceText] = Field(description='仅引用本次提供的任务来源标签，不得重复；只整理旧摘要时为空。')
    previous_topic_ids: list[WorkspaceText] = Field(description='引用给定旧主题的标识，不得重复；没有旧来源时为空。')

    @model_validator(mode='after')
    def validate_sources(self):
        if not self.task_ids and not self.previous_topic_ids:
            raise ValueError('主题至少需要一种有效来源')
        for values in (self.task_ids, self.previous_topic_ids):
            if len(values) != len(set(values)):
                raise ValueError('主题引用不得重复')
        return self


class FinishPlanningArguments(WorkspaceObject):
    summary: WorkspaceText = Field(max_length=1000, description='简述筛选结果；空主题时说明无需保留的原因，不替代实际摘要。')


class SummaryItem(WorkspaceObject):
    content: WorkspaceText = Field(description='精简业务内容，保留条件、冲突、实际状态和必要限制，不包含系统提示。')
    task_ids: list[WorkspaceText] = Field(description='实际支持该内容的本次任务来源标签，不得重复；只来自旧摘要时为空。')
    sources: list[WorkspaceText] = Field(description='资料中已有的可定位出处；没有可用来源时为空，不编造链接、页码或编号。')

    @model_validator(mode='after')
    def unique_references(self):
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError('摘要引用不得重复')
        return self


class TopicSummaryContent(WorkspaceObject):
    current_memory: list[SummaryItem] = Field(description='仍有效的事实、条件、必要限制及重要交付状态；省略失效状态和常规过程，无内容时为空。')
    open_items: list[SummaryItem] = Field(description='未解决问题、必要前提和暂停状态；删除已解决或撤销事项，不新增计划，无内容时为空。')


class TopicSummary(TopicSummaryContent):
    topic_id: WorkspaceText = Field(description='程序填写的主题标识。')
    title: WorkspaceText = Field(description='程序填写的主题名称。')


class TopicGenerationOutput(WorkspaceObject):
    summary: TopicSummaryContent = Field(description='依据分配资料生成的两区域记忆，不输出主题身份或推理字段。')


class TopicMemory(WorkspaceObject):
    format: Literal['topic_memory'] = 'topic_memory'
    topics: list[TopicSummary] = Field(max_length=32)

    @model_validator(mode='after')
    def unique_topics(self):
        if len({t.topic_id for t in self.topics}) != len(self.topics):
            raise ValueError('摘要主题不得重复')
        return self


def planning_tools():
    definitions = []
    for name, model, description in (
        ('extract_topic', SummaryTopic, '选择一个有继续使用价值的记忆主题及必要来源，给出简要提取范围；新标识追加，已有标识完整替换，最多32个。'),
        ('finish_topic_planning', FinishPlanningArguments, '确认主题筛选结束，允许空主题；未引用旧主题不会自动保留。'),
    ):
        schema = model.model_json_schema()
        check_descriptions(schema)
        definitions.append({'type': 'function', 'function': {'name': name,
            'description': description, 'parameters': schema, 'strict': False}})
    return definitions


def generation_schema():
    schema = TopicGenerationOutput.model_json_schema()
    check_descriptions(schema)
    def visit(value):
        if isinstance(value, dict):
            # 解码器可能将搜索正则解释为完整匹配；非空白仍由本地模型严格验收。
            if value.get('pattern') == r'\S':
                value.pop('pattern')
                value['minLength'] = 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return schema
