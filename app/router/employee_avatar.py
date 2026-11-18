"""仅已登录的注册中心管理员可按员工补齐头像，不接受用户自报权限。"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.requests import ClientDisconnect

from app.router.dependency import AuthenticatedUserDependency, user_bearer
from app.schema.employee_avatar import FillEmployeeAvatarRequest, FillEmployeeAvatarResponse
from app.service.employee_avatar import AvatarInputError, AvatarEmployeeNotFound

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/internal/employee', tags=['employee-avatar'])
_HEADERS = {'Cache-Control': 'no-store'}


@router.post('/avatar', response_model=FillEmployeeAvatarResponse,
    dependencies=[Depends(user_bearer)], summary='注册中心管理员补齐员工头像',
    openapi_extra={'requestBody': {'required': True, 'content': {
        'application/json': {'schema': FillEmployeeAvatarRequest.model_json_schema()}}}})
async def fill_avatar(request: Request, response: Response, user: AuthenticatedUserDependency):
    service = getattr(request.app.state, 'employee_avatar_service', None)
    if service is None:
        raise HTTPException(503, '头像补齐服务暂不可用', headers=_HEADERS)
    try:
        if not await service.require_registry_admin(user.userid):
            raise HTTPException(403, '仅注册中心管理员可以补齐员工头像', headers=_HEADERS)
        if request.query_params or request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(422, '仅接受JSON请求体，不接受查询参数', headers=_HEADERS)
        # 认证和授权完成后才读体；手工限流式请求大小，避免自动校验回显带签名URL。
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 16384:
                raise HTTPException(413, '头像补齐请求体超过16KiB限制', headers=_HEADERS)
        try:
            body = FillEmployeeAvatarRequest.model_validate_json(content)
        except ValueError:
            raise HTTPException(422, '请求体必须提供合法userid和avatar_url，不接受额外字段', headers=_HEADERS) from None
        result = await service.fill(body.userid, body.avatar_url)
    except HTTPException:
        raise
    except AvatarEmployeeNotFound:
        raise HTTPException(404, '员工不存在，请先完成员工资料同步', headers=_HEADERS) from None
    except AvatarInputError:
        raise HTTPException(422, '头像地址或图片内容不符合允许域名、格式及大小要求', headers=_HEADERS) from None
    except ClientDisconnect:
        raise HTTPException(499, '客户端已断开请求', headers=_HEADERS) from None
    except Exception as error:
        logger.warning('员工头像补齐失败：error_type=%s', type(error).__name__)
        raise HTTPException(503, '头像补齐未确认，请稍后重试；已有头像不会覆盖', headers=_HEADERS) from None
    response.headers.update(_HEADERS)
    return result
