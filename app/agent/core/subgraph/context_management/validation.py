"""工作区整理的保护路径、预留位置与既有结论状态校验。"""
from ...workspace import WorkspacePayload
from ...json_codec import ModelActionRejected


def get_path(data,path):
    if not isinstance(path,str) or not path.startswith('/') or any(not p for p in path.split('/')[1:]):
        raise ValueError('保护路径无效')
    for part in path.split('/')[1:]:
        if not isinstance(data,dict) or part not in data:
            raise ModelActionRejected('整理涉及的保护路径已不存在。', '保留原工作区的保护条目及其父容器，只整理未受保护内容；不要删除保护目标。')
        data=data[part]
    return data


def protection_paths(protected):
    # 既有内部调用的区域/条目格式在边界转换，执行与提示词只使用绝对路径。
    result={}
    for key,value in (protected or {}).items():
        if key.startswith('/'):
            result[key]=value
        else:
            result.update({f'/{key}/{entry}':item for entry,item in value.items()})
    return result


def validate_workspace_compression(original,candidate,protected=None,reserved_path=None):
    candidate=WorkspacePayload.model_validate(candidate.model_dump()).model_copy(deep=True)
    data=candidate.model_dump()
    for path,value in protection_paths(protected).items():
        if get_path(data,path)!=value:
            raise ModelActionRejected('整理改变了保护目标或其引用。', '恢复保护值及其引用，只合并或精简未受保护内容；本次修改未接受。')
    if reserved_path:
        parent,key=reserved_path.rsplit('/',1)
        if key in get_path(data,parent):
            raise ModelActionRejected('整理占用了待处理操作的预留位置。', '保持预留位置为空，不在该位置新增条目；整理其他已有内容。')
    for key,item in candidate.known_information.items():
        before=original.known_information.get(key)
        if before and item.status!=before.status:
            raise ModelActionRejected('整理改变了已有信息的确认状态。', '保留原 status，只精简内容和来源表述；压缩不提供新的核验依据。')
    for key,item in candidate.explored_directions.items():
        before=original.explored_directions.get(key)
        if before is None or item.outcome!=before.outcome:
            raise ModelActionRejected('整理创建了新探索成果或改变了既有 outcome。', '保留原有探索结果；只精简已记录内容，不开展或补写新的业务探索。')
    return candidate
