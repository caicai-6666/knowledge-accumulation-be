"""引用图片优先复用当前个人助理已有附件；不保存副本，不扩大查看权限。"""
import asyncio
import hashlib
import json

from app.infrastructure.robot_attachment import AttachmentRejected, validate_pdf


class RobotReplyFileResolver:
    def __init__(self, repository, nodes, files, *, platform_code):
        self.repository, self.nodes, self.files = repository, nodes, files
        self.platform_code = platform_code

    async def resolve(self, message):
        reply = message.reply
        if reply is None or reply.message_id is None or not reply.parts or reply.parts[0].type != 'picture':
            return None
        # 与原消息 request_id 使用相同算法，关联限定所属用户、本平台和当前助理树。
        identity = json.dumps([message.corp_id, message.robot_code, reply.message_id], ensure_ascii=False)
        request_id = 'dingtalk:' + hashlib.sha256(identity.encode()).hexdigest()
        found = await self.repository.find_message_attachment(message.userid, request_id,
            platform_code=self.platform_code)
        if found is None:
            return None
        tree_id, reference = found
        try:
            owned = await self.nodes.find_file(tree_id, message.userid, reference.file_id)
            if owned != reference:
                return None
            data = await self.files.read(reference)
            await asyncio.to_thread(validate_pdf, data)
        except (LookupError, OSError, AttachmentRejected):
            return None  # 未命中可用文件，继续走本次回调提供的下载码，不猜测其他文件。
        # 这里只提供已验证 ID；实际查看仍走公开状态门禁，不自动公开私密文件。
        return reference

    async def resolve_richtext(self, message):
        reply = message.reply
        if reply is None or reply.message_id is None or reply.from_assistant:
            return None
        identity = json.dumps([message.corp_id, message.robot_code, reply.message_id], ensure_ascii=False)
        request_id = 'dingtalk:' + hashlib.sha256(identity.encode()).hexdigest()
        found = await self.repository.find_message_content(message.userid, request_id,
            platform_code=self.platform_code)
        if found is None:
            return None
        tree_id, text, references = found
        # 必须整组可读，不能用本地残缺附件替代完整引用；不覆盖当前文件公开状态。
        try:
            for reference in references:
                if await self.nodes.find_file(tree_id, message.userid, reference.file_id) != reference:
                    return None
                data = await self.files.read(reference)
                await asyncio.to_thread(validate_pdf, data)
        except (LookupError, OSError, AttachmentRejected):
            return None
        return text, references
