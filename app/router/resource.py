"""已登录用户读取头像和本人任务附件；不创建登录或按客户端路径访问文件。"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from app.router.dependency import AuthenticatedUserDependency, reject_extra_input, user_bearer
from app.schema.auth import AuthErrorResponse
from app.model.question_tree import TreeID
from app.model.question_file import FileID
from app.repository.question_tree import QuestionTreeNotFound
from app.repository.node_persistence import QuestionFileNotFound

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/resource', tags=['resource'])
_HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


@router.get('/avatar/{filename:path}', response_class=Response,
            summary='读取头像', dependencies=[Depends(user_bearer)], responses={
                200: {'description': 'WebP 头像原始字节', 'content': {'image/webp': {'schema': {'type': 'string', 'format': 'binary'}}}},
                401: {'description': '用户 token 无效或中央会话失效', 'model': AuthErrorResponse},
                404: {'description': '头像不存在', 'model': AuthErrorResponse},
                422: {'description': '头像名称或请求参数不合法', 'model': AuthErrorResponse},
                503: {'description': '认证未启用、依赖不可用或头像读取失败', 'model': AuthErrorResponse},
            })
async def avatar(request: Request, user: AuthenticatedUserDependency,
                 filename: Annotated[str, Path(min_length=37, max_length=37, pattern=r'^[a-f0-9]{32}\.webp$', description='32 位小写十六进制标识加 .webp；不得包含目录或其他后缀。')]):
    await reject_extra_input(request)
    # 部分路由正则可忽略末尾换行；对照解码后的原始路径，禁止参数截断后绕过名称校验。
    if request.scope['path'].rsplit('/', 1)[-1] != filename:
        raise HTTPException(422, '请求参数不合法', headers=_HEADERS)
    store = getattr(request.app.state, 'avatar_store', None)
    if store is None:
        raise HTTPException(503, '头像读取暂不可用', headers=_HEADERS)
    try:
        # Store 使用受限目录描述符、O_NOFOLLOW 和大小上限；不按客户端路径直接打开文件。
        async with asyncio.timeout(store.settings.timeout_seconds):
            content = await store.read('user/avatar/' + filename)
    except FileNotFoundError:
        raise HTTPException(404, '头像不存在', headers=_HEADERS) from None
    except Exception as error:
        logger.warning('头像读取失败：error_type=%s', type(error).__name__)
        raise HTTPException(503, '头像读取暂不可用', headers=_HEADERS) from None
    return Response(content, media_type='image/webp', headers=_HEADERS)



_MEDIA_TYPES = {'images': 'application/pdf', 'audio': 'audio/mpeg', 'video': 'video/mp4'}


@router.get('/attachment/{tree_id}/{file_id:path}', response_class=Response,
            summary='读取本人任务附件', dependencies=[Depends(user_bearer)], responses={
                200: {'description': '已登记附件的原始字节', 'content': {
                    mime: {'schema': {'type': 'string', 'format': 'binary'}} for mime in _MEDIA_TYPES.values()}},
                401: {'description': '用户 token 无效或中央会话失效', 'model': AuthErrorResponse},
                404: {'description': '树或附件不存在，或不属于当前用户', 'model': AuthErrorResponse},
                422: {'description': '树 ID、文件 ID 或请求参数不合法', 'model': AuthErrorResponse},
                503: {'description': '认证、数据库或文件读取暂不可用', 'model': AuthErrorResponse},
            })
async def attachment(request: Request, user: AuthenticatedUserDependency,
                     tree_id: Annotated[TreeID, Path(min_length=32, max_length=32, description='附件所属问答树的 32 位小写十六进制 ID。')],
                     file_id: Annotated[FileID, Path(min_length=36, max_length=36,
                         description='任务 attachments 中的完整小写带连字符 UUID，不含后缀或目录。')]):
    await reject_extra_input(request)
    if request.scope['path'].rsplit('/', 1)[-1] != file_id:
        raise HTTPException(422, '请求参数不合法', headers=_HEADERS)
    service = getattr(request.app.state, 'question_file_service', None)
    try:
        if service is None:
            raise RuntimeError('附件服务未装配')
        # 复用数据库登记及读取前后归属核对；UUID 本身不是授权凭据。
        # 限制整条读取链路等待时间，不触发模型、上传或转码。
        async with asyncio.timeout(15):
            reference, content = await service.read_file(tree_id, user.userid, file_id)
    except (QuestionTreeNotFound, QuestionFileNotFound, FileNotFoundError):
        raise HTTPException(404, '附件不存在', headers=_HEADERS) from None
    except Exception as error:
        logger.warning('附件读取失败：error_type=%s', type(error).__name__)
        raise HTTPException(503, '附件读取暂不可用', headers=_HEADERS) from None
    return Response(content, media_type=_MEDIA_TYPES[reference.type], headers={
        **_HEADERS, 'Content-Disposition': f'inline; filename="{reference.filename}"'})
