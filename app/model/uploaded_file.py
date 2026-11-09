"""上传文件元信息；归属由宿主身份提供，不接受模型指定。"""
from datetime import timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.model.question_file import FileID, FileType

FileVisibility = Literal['public', 'private']
FileOwnerID = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'\S')]


class UploadedFileRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    id: FileID
    owner_userid: FileOwnerID
    type: FileType
    visibility: FileVisibility = 'public'
    uploaded_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator('uploaded_at', 'updated_at')
    @classmethod
    def normalize_dates(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def validate_times(self):
        if self.updated_at < self.uploaded_at:
            raise ValueError('文件元信息更新时间不能早于上传时间')
        return self
