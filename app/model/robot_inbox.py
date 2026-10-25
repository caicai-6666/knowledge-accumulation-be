"""机器人入站账本只保存可靠重试所需资料，不保存下载码或平台凭据。"""
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from app.model.assistant import AssistantModel, UserID, ExternalID
from app.model.question_file import AttachmentReference, FileID


class RobotInboxFile(AssistantModel):
    file_id: FileID
    sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    size: int | None = Field(default=None, strict=True, gt=0)

    @property
    def reference(self):
        return AttachmentReference(type='images',file_id=self.file_id)

    @model_validator(mode='after')
    def paired(self):
        if (self.sha256 is None)!=(self.size is None):raise ValueError('附件校验资料不完整')
        return self


class RobotInboxRecord(AssistantModel):
    request_id: ExternalID
    userid: UserID
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    message: str = Field(min_length=1,max_length=10000,repr=False)
    files: tuple[RobotInboxFile,...] = ()
    status: Literal['received','publish_pending','publish_unknown','publish_blocked','published',
                    'reply_pending','reply_unknown','rejected'] = 'received'
    reply_text: str | None = Field(default=None,max_length=1000)
    publish_started_at: AwareDatetime | None = None
    next_attempt_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
