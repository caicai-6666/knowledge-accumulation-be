"""个人助理附件转换落盘后的元信息登记，以及历史文件清理协调。"""
from pydantic import AwareDatetime, TypeAdapter

from app.model.question_file import AttachmentReference, FileID
from app.model.question_tree import utc_now
from app.model.uploaded_file import FileOwnerID, FileVisibility, UploadedFileRecord

_TIMESTAMP = TypeAdapter(AwareDatetime)
_FILE_ID = TypeAdapter(FileID)
_OWNER_ID = TypeAdapter(FileOwnerID)
_VISIBILITY = TypeAdapter(FileVisibility)


class UploadedFileRegistrationService:
    def __init__(self, repository, store, *, clock=utc_now):
        self.repository, self.store, self.clock = repository, store, clock

    async def register(self, reference: AttachmentReference, owner_userid: str, *, uploaded_at=None):
        """仅接收可信宿主已落盘的最终引用，不登记机器人临时副本或创建备忘。"""
        reference = AttachmentReference.model_validate(reference)
        now = _TIMESTAMP.validate_python(self.clock())
        uploaded_at = now if uploaded_at is None else _TIMESTAMP.validate_python(uploaded_at)
        record = UploadedFileRecord(id=reference.file_id, owner_userid=owner_userid,
            type=reference.type, uploaded_at=uploaded_at, updated_at=max(uploaded_at, now))
        return await self.repository.register(record)

    async def delete(self, reference: AttachmentReference, owner_userid: str):
        reference = AttachmentReference.model_validate(reference)
        # 元信息删除失败/结果不确定时保留文件。后续重试允许元信息或文件已不存在。
        await self.repository.remove(reference, owner_userid)
        await self.store.delete(reference)


class FileVisibilityService:
    """按可信所属用户管理文件范围；只改元信息，不调整任务引用或备忘。"""
    def __init__(self, repository, *, clock=utc_now):
        self.repository, self.clock = repository, clock

    async def get(self, file_id, owner_userid):
        return await self.repository.get_owned(_FILE_ID.validate_python(file_id), _OWNER_ID.validate_python(owner_userid))

    async def set(self, file_id, owner_userid, visibility):
        return await self.repository.set_visibility(_FILE_ID.validate_python(file_id),
            _OWNER_ID.validate_python(owner_userid), _VISIBILITY.validate_python(visibility),
            now=_TIMESTAMP.validate_python(self.clock()))
