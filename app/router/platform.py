"""已登录用户查询平台基础信息及数据库管理员展示资料。"""

from fastapi import APIRouter, Depends, Request, Response

from app.auth.error import AuthUnavailable
from app.router.dependency import AuthenticatedUserDependency, reject_extra_input, user_bearer
from app.schema.auth import AuthErrorResponse
from app.schema.platform import PlatformResponse

router = APIRouter(prefix='/platform', tags=['platform'], responses={
    401: {'description': '用户 token 无效、过期或中央登录失效', 'model': AuthErrorResponse},
    422: {'description': '请求参数不合法，不回显输入', 'model': AuthErrorResponse},
    503: {'description': '认证、平台目录或数据库依赖不可用', 'model': AuthErrorResponse},
})


@router.get('/list', response_model=list[PlatformResponse], summary='查询平台信息及管理员列表',
            dependencies=[Depends(user_bearer)])
async def list_platforms(request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    service = getattr(request.app.state, 'platform_query_service', None)
    if service is None:
        raise AuthUnavailable('平台信息暂不可用')
    platforms = await service.list_platforms()
    response.headers['Cache-Control'] = 'no-store'
    return platforms
