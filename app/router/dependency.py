"""只验证身份的可复用依赖，与显式登录交换入口分离。"""

import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from app.auth.error import AuthUnavailable
from app.auth.model import AuthenticatedUser
from app.service.auth import AuthService


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, 'auth_service', None)
    if service is None:
        raise AuthUnavailable('中间件接入未配置，用户认证暂不可用')
    return service


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
user_bearer = HTTPBearer(auto_error=False, scheme_name='UserBearer', bearerFormat='opaque')


def unauthorized():
    return HTTPException(status_code=401, detail='用户 token 无效、过期或中央登录已离线',
                         headers={'WWW-Authenticate':'Bearer', 'Cache-Control':'no-store'})


def bearer_token(request: Request) -> str:
    values = request.headers.getlist('authorization')
    if len(values) != 1 or re.fullmatch(r'(?i:Bearer) [!-~]{1,4096}', values[0]) is None:
        raise unauthorized()
    return values[0].split(' ', 1)[1]


async def reject_extra_input(request: Request, *, allow_body: bool = False) -> None:
    if request.query_params or (not allow_body and await request.body()):
        raise HTTPException(status_code=422, detail='请求参数不合法', headers={'Cache-Control':'no-store'})


async def require_authenticated_user(request: Request, service: AuthServiceDependency) -> AuthenticatedUser:
    """普通业务仅允许使用 access_token 查询中央登录，不创建或恢复登录。"""
    user = await service.resolve_user(bearer_token(request))
    if user is None:
        raise unauthorized()
    return user


AuthenticatedUserDependency = Annotated[AuthenticatedUser, Depends(require_authenticated_user)]
