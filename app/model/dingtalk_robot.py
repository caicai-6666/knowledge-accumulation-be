"""钉钉单聊业务输入；平台凭据和临时会话 Webhook 不进入规范化消息。"""
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.config.dingtalk_robot import DingTalkRobotSettings

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r'^\S+$')]


class RobotModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)


class RobotPart(RobotModel):
    type: Literal['text', 'picture', 'file', 'audio', 'video', 'unsupported']
    text: str | None = Field(default=None, repr=False, strict=True)
    download_code: SecretStr | None = Field(default=None, exclude=True, repr=False)
    filename: str | None = Field(default=None, strict=True)

    @model_validator(mode='after')
    def validate_part(self):
        if self.type == 'text':
            if not self.text or not self.text.strip() or self.download_code is not None or self.filename is not None:
                raise ValueError('文本片段必须只包含有效正文')
        elif self.type == 'unsupported':
            if self.text is not None or self.download_code is not None or self.filename is not None:
                raise ValueError('不支持的消息只保存类型标记')
        elif (self.text is not None or self.download_code is None or not self.download_code.get_secret_value().strip()
              or (self.type == 'file' and (not self.filename or not self.filename.strip()))):
            raise ValueError('附件片段必须有有效下载码，文件消息还需原文件名')
        return self


class RobotMessage(RobotModel):
    message_id: Identifier
    corp_id: Identifier
    robot_code: Identifier
    userid: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^\S+$')]
    conversation_id: Identifier
    created_at: AwareDatetime
    message_type: Literal['text', 'picture', 'richText', 'file', 'audio', 'video', 'unknownMsgType']
    parts: tuple[RobotPart, ...] = Field(repr=False, min_length=1)

    @property
    def request_id(self) -> str:
        # msgId 可能包含特殊字符；组合企业/机器人身份生成稳定、短且不泄漏原始消息的入队去重键。
        value = json.dumps([self.corp_id, self.robot_code, self.message_id], ensure_ascii=False)
        return 'dingtalk:' + hashlib.sha256(value.encode()).hexdigest()


class RobotMessageInvalid(ValueError):
    def __init__(self, reason='invalid_message'):
        super().__init__('钉钉机器人消息格式不合法')
        self.reason = reason


def _text(value, *, reason='text_content'):
    if not isinstance(value, str) or not value.strip():
        raise RobotMessageInvalid(reason)
    return value


def _file(kind, content):
    # 钉钉图片允许使用 pictureDownloadCode；不能把合法图片误判为缺少下载码。
    code = content.get('downloadCode')
    if kind == 'picture' and (not isinstance(code, str) or not code.strip()):
        code = content.get('pictureDownloadCode')
    if not isinstance(code, str) or not code.strip():
        raise RobotMessageInvalid('attachment_download_code')
    filename = content.get('fileName')
    if kind == 'file':
        filename = _text(filename, reason='attachment_filename')
    elif filename is not None and not isinstance(filename, str):
        raise RobotMessageInvalid('attachment_filename')
    # filename 只作展示元数据，绝不能作为本地保存路径。
    return RobotPart(type=kind, download_code=SecretStr(code), filename=filename)


def parse_robot_message(payload: dict, settings: DingTalkRobotSettings) -> RobotMessage | None:
    """只供已认证的 Stream 连接调用；群聊/外企业/其他机器人返回 None，禁止用作公开 HTTP 鉴权。"""
    stage = 'envelope'
    try:
        if not isinstance(payload, dict):
            raise RobotMessageInvalid()
        if payload.get('conversationType') != '1':
            return None
        if (payload.get('chatbotCorpId') != settings.corp_id or payload.get('senderCorpId') != settings.corp_id
                or payload.get('robotCode') != settings.robot_code):
            return None
        kind = payload.get('msgtype')
        stage = 'content_object'
        content = payload.get('content', {})
        if not isinstance(content, dict):
            raise RobotMessageInvalid()
        if kind == 'text':
            stage = 'text_content'
            parts = [RobotPart(type='text', text=_text(payload['text']['content']))]
        elif kind == 'unknownMsgType':
            # 钉钉无法透传的消息也必须进入可靠拒绝链路；协议 500 不会变成聊天提示。
            # 不保存上游提示正文，不从缺失的下载码猜测附件，更不能继续发布部分文字。
            parts = [RobotPart(type='unsupported')]
        elif kind in ('picture', 'file', 'audio', 'video'):
            parts = [_file(kind, content)]
        elif kind == 'richText':
            stage = 'rich_text_list'
            items = content['richText']
            if not isinstance(items, list) or not items:
                raise RobotMessageInvalid(stage)
            parts = []
            for index, item in enumerate(items):
                stage = f'rich_text_item_{index}'
                if not isinstance(item, dict):
                    raise RobotMessageInvalid(stage)
                if item.get('type') in ('picture', 'file', 'audio', 'video'):
                    # 已知附件即使业务不支持也完整传给接收器，由它拒绝整条并反馈用户。
                    parts.append(_file(item['type'], item))
                elif set(item) <= {'text', 'type'} and item.get('type', 'text') == 'text':
                    text = item.get('text')
                    if not isinstance(text, str):
                        raise RobotMessageInvalid(f'{stage}_text_type')
                    # 富文本编辑器可能附带空串或换行占位；它们没有业务正文，不能使合法图片整条失败。
                    # 只忽略明确的空白字符串，缺字段、非字符串和未知片段仍必须失败。
                    if text.strip():
                        parts.append(RobotPart(type='text', text=text))
                else:
                    # 不丢弃不认识的片段后继续入队，避免把半条消息当作完整输入。
                    raise RobotMessageInvalid(stage)
            if not parts:
                raise RobotMessageInvalid('rich_text_empty')
        else:
            raise RobotMessageInvalid('message_type')
        stage = 'timestamp'
        timestamp = payload['createAt']
        if type(timestamp) is not int or timestamp <= 0:
            raise RobotMessageInvalid(stage)
        stage = 'message_identity'
        return RobotMessage(message_id=payload['msgId'], corp_id=settings.corp_id, robot_code=settings.robot_code,
            userid=payload['senderStaffId'], conversation_id=payload['conversationId'],
            created_at=datetime.fromtimestamp(timestamp / 1000, timezone.utc), message_type=kind, parts=tuple(parts))
    except RobotMessageInvalid:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise RobotMessageInvalid(stage) from None
