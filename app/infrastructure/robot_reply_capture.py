"""按显式采样文件捕获一条指定用户的脱敏回调；不改变正常接收和 ACK。"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config.middleware import PROJECT_ROOT

# 只保留正文及消息关联值；下载码、Webhook、凭据和未知标量仅保留类型。
_VALUE_KEYS = {'msgId', 'originalMsgId', 'msgtype', 'msgType', 'type', 'text',
               'content', 'fileName', 'createAt', 'isReplyMsg', 'elementType', 'originalProcessQueryKey'}


def sanitized_callback(value, *, key='', depth=0, in_card=False):
    if depth > 12:
        return '<depth-limit>'
    in_card = in_card or key == 'cardContent'
    if isinstance(value, dict):
        result = {}
        for k, v in list(value.items())[:100]:
            # 仅卡片 TEXT 节点的 value 是正文；图片链接、按钮参数和其他 value 不放开。
            if (in_card and k == 'value' and value.get('elementType') == 'TEXT'
                    and isinstance(v, str)):
                result[k] = v[:4000]
            else:
                result[str(k)[:120]] = sanitized_callback(v, key=k, depth=depth + 1, in_card=in_card)
        return result
    if isinstance(value, list):
        return [sanitized_callback(v, depth=depth + 1, in_card=in_card) for v in value[:30]]
    if key in _VALUE_KEYS and isinstance(value, (str, bool, int, float)):
        return value[:4000] if isinstance(value, str) else value
    return {'present': value is not None, 'type': type(value).__name__}


def capture_reply_callback(payload, settings, *, root: Path | None = None):
    """只有启用文件存在、未过期且身份匹配才采样；每次运行最多一条。"""
    directory = (root if root is not None else PROJECT_ROOT / 'experiment/dingtalk-reply-capture')
    armed = directory / 'armed.json'
    try:
        config = json.loads(armed.read_text())
    except FileNotFoundError:
        return
    expires = datetime.fromisoformat(config['expires_at'])
    if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
        return
    if (not isinstance(payload, dict) or payload.get('conversationType') != '1'
            or payload.get('chatbotCorpId') != settings.corp_id
            or payload.get('senderCorpId') != settings.corp_id
            or payload.get('robotCode') != settings.robot_code
            or payload.get('senderStaffId') != config['userid']):
        return
    run = config['run']
    if not isinstance(run, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', run):
        raise ValueError('采样目录名不合法')
    destination = directory / run
    destination.mkdir(parents=True, exist_ok=True)
    result = {'captured_at': datetime.now(timezone.utc).isoformat(),
              'redacted': True, 'capture_format': 2, 'payload': sanitized_callback(payload)}
    try:
        descriptor = os.open(destination / 'callback.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # 重推及后续消息不覆盖首次证据。
    with os.fdopen(descriptor, 'w') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
