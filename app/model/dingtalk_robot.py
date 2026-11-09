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


class RobotReply(RobotModel):
    """引用作为本条消息携带的资料，不从引用发送者推导权限或用户身份。"""
    message_id: Identifier | None = None
    kind: Literal['text', 'picture', 'richText', 'interactiveCard'] | None = None
    from_assistant: bool = False
    needs_local: bool = False
    parts: tuple[RobotPart, ...] = Field(default=(), repr=False)
    rejection: str | None = Field(default=None, repr=False)


class RobotMessage(RobotModel):
    message_id: Identifier
    corp_id: Identifier
    robot_code: Identifier
    userid: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^\S+$')]
    conversation_id: Identifier
    created_at: AwareDatetime
    message_type: Literal['text', 'picture', 'richText', 'file', 'audio', 'video', 'unknownMsgType']
    parts: tuple[RobotPart, ...] = Field(repr=False, min_length=1)

    reply: RobotReply | None = None

    @property
    def task_parts(self):
        # 引用图片排在本次新增附件前；稳定顺序供暂存与重推映射使用。
        return (self.reply.parts if self.reply is not None else ()) + self.parts

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



def _card_text(content):
    """只接收实测的文字卡片结构；同一段的 TEXT 必须拼接，不能逐样式节点换行。"""
    def visit(node, depth=0):
        if depth > 12 or not isinstance(node, dict):
            raise ValueError('卡片结构不支持')
        kind = node.get('elementType')
        if kind == 'TEXT' and isinstance(node.get('value'), str) and 'children' not in node:
            return node['value']
        if kind == 'RICHTEXT' and isinstance(node.get('children'), list) and node['children']:
            return ''.join(visit(child, depth + 1) for child in node['children'])
        raise ValueError('卡片含非文字或未知节点')
    blocks = content.get('cardContent')
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('卡片正文缺失')
    return _text('\n\n'.join(visit(block) for block in blocks))


def _rich_reply_parts(content):
    """保留文字/图片片段顺序，未知片段整条拒绝；兼容引用与普通富文本字段形态。"""
    items = content.get('richText')
    if not isinstance(items, list) or not items:
        raise ValueError('引用富文本没有有效片段')
    parts = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('引用富文本片段不合法')
        kind = item.get('msgType', item.get('type', 'text' if 'text' in item else None))
        if 'msgType' in item and 'type' in item and item['msgType'] != item['type']:
            raise ValueError('引用富文本片段类型冲突')
        body = item.get('content', item)
        if kind == 'text':
            if isinstance(body, dict):
                value = body.get('text', body.get('content'))
            else:
                value = body
            if not isinstance(value, str):
                raise ValueError('引用富文本缺少文字')
            if value.strip():
                parts.append(RobotPart(type='text', text=value))
        elif kind == 'picture' and isinstance(body, dict):
            parts.append(_file('picture', body))
        else:
            raise ValueError('引用富文本包含不支持的片段')
    if not parts:
        raise ValueError('引用富文本为空')
    return tuple(parts)


def _parse_reply(payload):
    # 实测引用位于 text.repliedMsg；同时检查 content 容器，避免富文本引用被静默忽略。
    envelopes = [value for key in ('text', 'content')
                 if isinstance(value := payload.get(key), dict)
                 and ('repliedMsg' in value or value.get('isReplyMsg') is True)]
    if not envelopes:
        if payload.get('originalMsgId'):
            return RobotReply(rejection='这条回复缺少被引用消息的内容，本条消息未提交给助理。请重新引用原消息，或直接发送文字/图片并附上问题。')
        return None
    if len(envelopes) != 1:
        return RobotReply(rejection='这条回复的引用信息不明确，本条消息未提交给助理。请只引用一条文字或图片消息后重新发送。')
    quoted = envelopes[0].get('repliedMsg')
    if not isinstance(quoted, dict):
        return RobotReply(rejection='未收到被引用消息的有效内容，本条消息未提交给助理。请重新引用，或直接发送原文字/图片及问题。')
    kind = quoted.get('msgType')
    if kind not in ('text', 'picture', 'richText', 'interactiveCard'):
        return RobotReply(rejection='暂不支持这类引用消息，目前支持文字、图片、图文及本助理的文字回复；文件、语音、视频和其他卡片暂不支持。本条消息未提交给助理，请直接发送原文字或图片并附上问题。')
    original, identity = payload.get('originalMsgId'), quoted.get('msgId')
    if original is not None and identity is not None and original != identity:
        return RobotReply(rejection='引用消息的标识不一致，本条消息未提交给助理。请重新选择原消息进行回复。')
    content = quoted.get('content')
    if not isinstance(content, dict):
        return RobotReply(rejection='被引用消息缺少可读取的正文，本条消息未提交给助理。请直接发送原文字或图片并附上问题。')
    from_assistant = isinstance(payload.get('originalProcessQueryKey'), str) and bool(payload['originalProcessQueryKey'].strip())
    try:
        if kind == 'interactiveCard':
            if not from_assistant:
                return RobotReply(rejection='目前仅支持引用本助理的文字回复，不支持其他卡片。本条消息未提交给助理，请复制需要讨论的原文后发送。')
            try:
                text = _card_text(content)
            except (ValueError, RobotMessageInvalid):
                return RobotReply(rejection='这条助理回复的卡片无法完整读取，可能包含非文字或不支持的结构。本条消息未提交给助理，请复制原文并附上问题。')
            return RobotReply(message_id=identity or original, kind=kind, from_assistant=True,
                              parts=(RobotPart(type='text', text=text),))
        if kind == 'richText':
            try:
                parts = _rich_reply_parts(content)
            except (ValueError, RobotMessageInvalid):
                return RobotReply(rejection='被引用图文内容不完整，或包含文字和图片以外的片段，本条消息未提交给助理。请直接重发原文字和图片，并附上问题。')
            # 已实测钉钉返回的占位内容；不得把平台提示伪装为用户原文。
            placeholder = (len(parts) == 1 and parts[0].type == 'text'
                           and parts[0].text.strip() == '版本过低不支持展示，请升级客户端')
            return RobotReply(message_id=identity or original, kind=kind, from_assistant=from_assistant,
                              needs_local=placeholder, parts=() if placeholder else parts)
        if kind == 'text':
            # 两种已知文字字段均支持；都提供且内容冲突时不猜测其含义。
            candidates = [content[key] for key in ('content', 'text') if key in content]
            if not candidates or any(not isinstance(v, str) or not v.strip() for v in candidates):
                return RobotReply(rejection='被引用文字没有有效原文，本条消息未提交给助理。请复制原文并附上本次问题后重新发送。')
            if len(set(candidates)) != 1:
                return RobotReply(rejection='被引用文字包含不一致的正文，本条消息未提交给助理。请复制需要讨论的原文并重新发送。')
            part = RobotPart(type='text', text=candidates[0])
        else:
            try:
                part = _file('picture', content)
            except RobotMessageInvalid:
                return RobotReply(rejection='被引用图片缺少有效下载信息，本条消息未提交给助理。请重新发送这张图片，并附上本次问题。')
        return RobotReply(message_id=identity or original, kind=kind, from_assistant=from_assistant, parts=(part,))
    except (ValueError, TypeError):
        return RobotReply(rejection='被引用消息的信息不完整，本条消息未提交给助理。请重新引用原文字或图片后发送。')


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
            created_at=datetime.fromtimestamp(timestamp / 1000, timezone.utc), message_type=kind, parts=tuple(parts), reply=_parse_reply(payload))
    except RobotMessageInvalid:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise RobotMessageInvalid(stage) from None
