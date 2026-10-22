"""显式用户登录与中央会话身份查询。"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.model import AuthenticatedUser
from app.router.dependency import AuthServiceDependency, bearer_token, reject_extra_input, unauthorized, user_bearer
from app.schema.auth import AuthErrorResponse, LoginRequest, LoginResponse

router = APIRouter(prefix='/auth', tags=['auth'], responses={
    401:{'description':'用户凭据无效、过期或中央登录离线', 'model':AuthErrorResponse},
    422:{'description':'请求参数不合法，不回显输入', 'model':AuthErrorResponse},
    503:{'description':'登录未启用、依赖不可用或中间件未授权', 'model':AuthErrorResponse},
})


@router.post('/login', response_model=LoginResponse, summary='显式登录并交换中央用户 token',
             dependencies=[Depends(user_bearer)], openapi_extra={'security':[{}, {'UserBearer':[]}]})
async def login(request: Request, service: AuthServiceDependency, response: Response,
                payload: LoginRequest | None = None):
    if service.settings.mode == 'development':
        await reject_extra_input(request)
        credential = bearer_token(request)
    else:
        await reject_extra_input(request, allow_body=True)
        if payload is None:
            raise HTTPException(status_code=422, detail='请求参数不合法', headers={'Cache-Control':'no-store'})
        credential = payload.code.get_secret_value()
    token, user, ttl = await service.login(credential)
    response.headers['Cache-Control'] = 'no-store'
    return LoginResponse(access_token=token, expires_in=ttl, user=user)


@router.get('/me', response_model=AuthenticatedUser, summary='只查询中央用户当前身份', dependencies=[Depends(user_bearer)])
async def current_user(request: Request, service: AuthServiceDependency, response: Response):
    await reject_extra_input(request)
    user = await service.resolve_current_user(bearer_token(request))
    if user is None:
        raise unauthorized()
    response.headers['Cache-Control'] = 'no-store'
    return user


