"""冻结主助手预演操作；先整理旧副本，再应用同一位置与值，不再生成ID。"""
from copy import deepcopy
from dataclasses import dataclass
from ....workspace import WorkspacePayload
from ..validation import get_path

WORKSPACE_TOOLS=frozenset({'workspace_add','workspace_replace','workspace_delete'})


@dataclass(frozen=True)
class PendingEdit:
    name: str
    path: str
    removed_path: str | None
    preview: WorkspacePayload
    protected: dict
    reserved_path: str | None


def freeze_edit(original,preview,name,arguments):
    before=original.model_dump()
    after=preview.model_dump()
    removed=None
    reserved=None
    protected={}
    path=arguments.path
    if name=='workspace_add':
        if arguments.direction_id is not None:
            path+='/'+arguments.direction_id
            removed='/remaining_directions/'+arguments.direction_id
            protected[removed]=deepcopy(get_path(before,removed))
        else:
            added=get_path(after,path).keys()-get_path(before,path).keys()
            if len(added)!=1:
                raise ValueError('新增预演必须有唯一程序分配ID')
            path+='/'+next(iter(added))
            reserved=path
    else:
        parts=path.split('/')[1:]
        target=path if parts[0]=='task_constraints' else '/'+'/'.join(parts[:2])
        protected[target]=deepcopy(get_path(before,target))
        if name=='workspace_delete':removed=path
    if name!='workspace_delete' and path.startswith('/explored_directions/'):
        for key in get_path(after,path.rsplit('/',1)[0] if len(path.split('/'))>3 else path).get('information_ids',[]):
            ref='/known_information/'+key
            protected[ref]=deepcopy(get_path(before,ref))
    return PendingEdit(name,path,removed,preview.model_copy(deep=True),protected,reserved)


def apply_pending(compressed,pending):
    data=compressed.model_dump()
    if pending.removed_path:
        parent,key=pending.removed_path.rsplit('/',1)
        del get_path(data,parent)[key]
    if pending.name!='workspace_delete':
        parent,key=pending.path.rsplit('/',1)
        get_path(data,parent)[key]=deepcopy(get_path(pending.preview.model_dump(),pending.path))
    return WorkspacePayload.model_validate(data)
