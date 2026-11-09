"""只准备所属用户在个人助理树中的 PDF 附件；不向其他文件系统扩展访问。"""
from app.repository.uploaded_file import UploadedFileAccessError

MAX_DELIVERY_FILE_BYTES = 20 * 1024 * 1024


class FileDeliveryRejected(ValueError):
    def __init__(self, reason, action):
        self.reason, self.action = reason, action
        super().__init__(reason + action)


class AssistantFileDeliveryService:
    def __init__(self, nodes, metadata, store):
        self.nodes, self.metadata, self.store = nodes, metadata, store

    async def load(self, userid, tree_id, file_id):
        try:
            reference = await self.nodes.find_file(tree_id, userid, file_id)
            record = await self.metadata.get_owned(file_id, userid)
        except UploadedFileAccessError as error:
            raise FileDeliveryRejected(error.reason, error.required_action) from None
        except (LookupError, PermissionError):
            raise FileDeliveryRejected('附件不存在于当前个人助理的可访问范围，本次未发送。',
                '请核对所属用户上传的附件 ID；不要使用注册中心文件 ID。移除无效引用并调整正文后重新提交。') from None
        if record.type != reference.type or reference.type != 'images':
            raise FileDeliveryRejected('当前附件类型不支持文件交付，本次未发送；目前仅支持 PDF。',
                '请移除该附件 ID 并说明限制，不要将音视频改名为 PDF。')
        try:
            content = await self.store.read(reference)
        except (OSError, ValueError):
            raise FileDeliveryRejected('附件内容当前无法读取，本次未发送。',
                '请核对文件是否仍可用，或移除该引用并在正文中说明未能发送。') from None
        if not content or not content.startswith(b'%PDF-'):
            raise FileDeliveryRejected('附件内容不是可发送的 PDF，本次未发送。',
                '请重新上传有效 PDF；移除该引用并调整正文。')
        if len(content) > MAX_DELIVERY_FILE_BYTES:
            raise FileDeliveryRejected('附件超过 20 MB，本次未发送。',
                '请提供不超过 20 MB 的 PDF；移除超限引用并调整正文。')
        # 发送回所属用户不扩大可见范围，私密文件也允许交付本人；读取后再次核对归属。
        try:
            current = await self.metadata.get_owned(file_id, userid)
            current_reference = await self.nodes.find_file(tree_id, userid, file_id)
        except UploadedFileAccessError as error:
            raise FileDeliveryRejected(error.reason, error.required_action) from None
        except (LookupError, PermissionError):
            raise FileDeliveryRejected('附件引用已失效，本次未发送。',
                '请核对文件 ID；移除失效引用并调整正文后重新提交。') from None
        if current.type != reference.type or current_reference != reference:
            raise FileDeliveryRejected('附件登记发生变化，本次未发送。','请核对文件 ID 后重新提交。')
        return reference.filename, content

    async def validate(self, userid, tree_id, file_ids):
        for file_id in file_ids:
            await self.load(userid, tree_id, file_id)
