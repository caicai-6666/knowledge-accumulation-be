"""个人背景的本轮注入契约；不改写树历史或权威工作区。"""
from typing import Annotated,Literal
from pydantic import BaseModel,ConfigDict,Field
from app.agent.core import ContextInjection

Text=Annotated[str,Field(strict=True,min_length=1,pattern=r'\S')]


class ContextMaterial(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    content: Text = Field(description='本次辅助回答所需的事实、偏好或历史结论原文。')
    source: Text = Field(description='资料的业务来源说明；不能使用内部运行或节点编号代替来源。')
    status: Literal['confirmed','user_reported','unverified','conflicted'] = Field(
        default='unverified',description='资料确认状态；用户陈述与待核实信息不得默认为已确认事实。')


class AssistantContext(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    profile: list[ContextMaterial] = Field(default_factory=list,description='经当前用户授权筛选的个人背景资料。')
    preferences: list[ContextMaterial] = Field(default_factory=list,description='当前用户的服务偏好及其来源。')
    relevant_memories: list[ContextMaterial] = Field(default_factory=list,description='与本次任务有关且允许当前用户读取的记忆片段。')


def build_context(base:ContextInjection,personal:AssistantContext | None=None) -> ContextInjection:
    result=ContextInjection.model_validate(base.model_dump())
    if personal is None:
        return result
    personal=AssistantContext.model_validate(personal.model_dump())
    if not any((personal.profile,personal.preferences,personal.relevant_memories)):
        return result
    # 所有背景作为普通资料注入；不会因包含指令、偏好或旧结论而获得系统角色。
    labels={'profile':'员工个人信息','preferences':'服务偏好','relevant_memories':'相关记忆'}
    statuses={'confirmed':'已确认的记录','user_reported':'用户陈述','unverified':'待核实','conflicted':'存在冲突'}
    sections=[result.supplemental] if result.supplemental else []
    for field,label in labels.items():
        for item in getattr(personal,field):
            # 来源与确认状态保持可见，正文直接展示，不再包装成 YAML 或 JSON。
            sections.append(f'【{label}】\n来源：{item.source}\n确认状态：{statuses[item.status]}\n{item.content}')
    return result.model_copy(update={'supplemental':'\n\n'.join(sections)})

__all__=['AssistantContext','ContextMaterial','build_context']
