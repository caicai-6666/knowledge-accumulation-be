"""只校验整理候选的结构保真和修改目标保护，不提交状态。"""


def validate_workspace_compression(original,candidate,protected=None):
    if original.task_constraints!=candidate.task_constraints:
        raise ValueError('整理不能改变用户任务和补充')
    for region in ('known_information','explored_directions','remaining_directions'):
        before=getattr(original,region);after=getattr(candidate,region)
        if before.keys()!=after.keys():
            raise ValueError('整理不能删除或新增条目身份')
    for key,item in original.known_information.items():
        other=candidate.known_information[key]
        if (item.source,item.status)!=(other.source,other.status):
            raise ValueError('整理不能改变事实来源或确认状态')
    for key,item in original.explored_directions.items():
        other=candidate.explored_directions[key]
        if (item.outcome,item.information_ids)!=(other.outcome,other.information_ids):
            raise ValueError('整理不能改变探索结果或引用')
    data=candidate.model_dump()
    for region,entries in (protected or {}).items():
        for key,value in entries.items():
            if data[region].get(key)!=value:
                raise ValueError('整理不能改写本次修改的目标条目')
