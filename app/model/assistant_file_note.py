"""个人文件备忘的存储约束；记录者由可信宿主绑定。"""
from typing import Annotated
from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, field_validator
from app.model.question_file import FileID
from app.model.uploaded_file import FileOwnerID

NoteName = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]
NoteSummary = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class AssistantFileNote(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    owner_userid: FileOwnerID
    file_id: FileID
    name: NoteName
    summary: NoteSummary
    updated_at: AwareDatetime

    @field_validator('summary')
    @classmethod
    def summary_capacity(cls, value):
        if len(value.encode('utf-8')) > 65535:
            raise ValueError('摘要超过 TEXT 容量，请精简到 UTF-8 编码不超过 65535 字节')
        return value


class FileNoteSearchRecord(AssistantFileNote):
    uploaded_at: AwareDatetime
