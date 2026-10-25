"""工作区整理的保护路径、预留位置与既有结论状态校验。"""
from ...workspace import WorkspacePayload


def get_path(data,path):
    if not isinstance(path,str) or not path.startswith('/') or any(not p for p in path.split('/')[1:]):
        raise ValueError('保护路径无效')
    for part in path.split('/')[1:]:
        if not isinstance(data,dict) or part not in data:
            raise ValueError('保护路径不存在')
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
            raise ValueError('整理不能改变保护目标及其引用')
    if reserved_path:
        parent,key=reserved_path.rsplit('/',1)
        if key in get_path(data,parent):
            raise ValueError('整理不能占用待处理操作的预留ID')
    for key,item in candidate.known_information.items():
        before=original.known_information.get(key)
        if before and item.status!=before.status:
            raise ValueError('整理不能改变已有信息确认状态')
    for key,item in candidate.explored_directions.items():
        before=original.explored_directions.get(key)
        if before is None or item.outcome!=before.outcome:
            raise ValueError('整理不能创建探索成果或改变已有结果')
    return candidate
