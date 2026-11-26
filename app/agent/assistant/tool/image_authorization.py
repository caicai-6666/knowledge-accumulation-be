"""仅为个人助理的本地 PDF 查看器追加公开状态校验，不扩展文件读取权限。"""
from __future__ import annotations

from inspect import isawaitable
import logging

from agent_core.core.file.pdf_viewer import PDFViewError
from agent_core.core.model import AgentDependencyError

from app.model.question_file import AttachmentReference
from app.repository.uploaded_file import UploadedFileAccessError

logger = logging.getLogger(__name__)


def wrap_image_authorize(authorize, *, userid: str, visibility):
    """包装本地附件鉴权回调：先校验树归属，再校验 `uploaded_files.visibility == 'public'`。"""

    async def guarded(file_id: str):
        # 原有任务树归属必须先通过；知道公开文件 ID 不构成跨用户读取授权。
        decision = authorize(file_id)
        decision = await decision if isawaitable(decision) else decision
        if not decision:
            return False
        expected_type = decision.type if isinstance(decision, AttachmentReference) else 'images'
        try:
            if visibility is None:
                raise RuntimeError('附件可见范围服务未装配')
            record = await visibility.get(file_id, userid)
        except UploadedFileAccessError as error:
            # 保留上游已确认的原因与修正方向，不从缺少登记推断文件不存在。
            raise PDFViewError(error.code, error.reason, error.required_action) from None
        except AgentDependencyError:
            raise
        except Exception as error:
            logger.warning('助理附件公开状态校验失败：error_type=%s', type(error).__name__)
            raise PDFViewError(
                'attachment_visibility_unavailable',
                '暂时无法确认附件的公开状态，本次未提供页面。',
                '请稍后重试，不要据此判断文件不存在或已公开。',
                failure_mode='failed',
            ) from None
        if (record.id, record.owner_userid, record.type) != (file_id, userid, expected_type):
            raise PDFViewError(
                'attachment_metadata_mismatch',
                '附件登记与当前引用不一致，本次未提供页面。',
                '请核对文件 ID，仍有问题时请管理员核查上传登记。',
            )
        if record.visibility != 'public':
            # 此限制也适用于所属用户本人上传的私密文件，不能为了查看自行扩大公开范围。
            raise PDFViewError(
                'attachment_not_public',
                '该附件处于私密状态，个人助理的附件查看工具仅允许查看公开文件，本次未提供页面。',
                '请向所属用户说明读取限制；只有用户明确要求公开时，才可调整可见范围后重新查看，不得为读取而自行公开文件。',
            )
        return decision

    return guarded


__all__ = ['wrap_image_authorize']
