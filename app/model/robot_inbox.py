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
    # 数据库首次登记分配，重推/重试不改变；不进入模型上下文。
    arrival_sequence: int | None = Field(default=None, strict=True, gt=0)
    request_id: ExternalID
    userid: UserID
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    message: str = Field(min_length=1,max_length=10000,repr=False)
    files: tuple[RobotInboxFile,...] = ()
    status: Literal['received','publish_pending','publish_unknown','publish_blocked','published',
                    'reply_pending','reply_unknown','rejected','cancelled'] = 'received'
    # 拒绝状态为待发送通知；cancelled 时仅保存人工终止原因，不发送通知。
    reply_text: str | None = Field(default=None,max_length=1000)
    publish_started_at: AwareDatetime | None = None
    next_attempt_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode='after')
    def validate_cancellation(self):
        if self.status == 'cancelled':
            if not self.reply_text or not self.reply_text.strip():
                raise ValueError('终止接收必须保留原因')
            if self.publish_started_at is not None:
                raise ValueError('尝试过发布的消息不能作为未发送消息终止')
        return self
