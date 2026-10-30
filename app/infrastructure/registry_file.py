"""受信任注册中心原件客户端；平台认证与可信任务用户分离，无重定向或代理。"""
import asyncio
import re
import httpx
from pydantic import SecretStr
from app.config.registry_file import RegistryFileSettings
from app.config.api import APPLICATION_PREFIX


class RegistryFileError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_resource_id(value):
    if (not isinstance(value,str) or re.fullmatch(r'[1-9][0-9]{0,19}',value) is None
            or int(value)>18446744073709551615):
        raise ValueError('文件ID须为注册中心正式资源ID的十进制字符串')
    return value


def validate_userid(value):
    if not isinstance(value,str) or not 1 <= len(value) <= 128 or not value.strip() or value != value.strip():
        raise ValueError('文件读取必须绑定有效任务用户')
    return value


class RegistryFileClient:
    def __init__(self, settings: RegistryFileSettings, *, sessions=None, transport=None):
        self.settings = settings
        self.sessions = sessions
        self._http = httpx.AsyncClient(timeout=settings.timeout_seconds,trust_env=False,
            follow_redirects=False,transport=transport)

    async def read_file(self, resource_id, *, userid):
        validate_resource_id(resource_id)
        validate_userid(userid)
        # 每次读取共享会话的当前令牌，重登后不会继续使用旧凭据；userid 不来自模型参数。
        access_token = self.sessions.get_access_token() if self.sessions is not None else None
        if (not isinstance(access_token,SecretStr)
                or re.fullmatch(r'[!-~]{1,4096}',access_token.get_secret_value()) is None):
            raise RegistryFileError('platform_session_required','平台会话暂不可用，请稍后重试。')
        if self.settings.base_url is None:
            raise RegistryFileError('registry_unavailable','尚未配置注册中心文件服务。')
        url=str(self.settings.base_url).rstrip('/')+f'{APPLICATION_PREFIX}/registry/api/resource/service-authorized-file/'+resource_id
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                # 状态先于正文处理，错误页和响应头不进入日志、模型或工具回执。
                async with self._http.stream('POST',url,json={'userid':userid},headers={
                    'Authorization':'Bearer '+access_token.get_secret_value(), 'Accept':'application/pdf',
                    'Accept-Encoding':'identity'}) as response:
                    if response.status_code == 401:
                        self.sessions.invalidate_access_token(access_token)
                    errors={401:('platform_session_expired','平台会话已失效，请稍后重试。'),
                        403:('file_forbidden','当前用户无权读取该文件。'),
                        404:('file_not_found','注册中心文件不存在或已被删除。'),
                        503:('registry_unavailable','注册中心文件服务暂不可用，请稍后重试。')}
                    if response.status_code in errors:
                        raise RegistryFileError(*errors[response.status_code])
                    if response.status_code!=200:
                        raise RegistryFileError('registry_unavailable','注册中心文件服务返回异常，请稍后重试。')
                    kind=response.headers.get('content-type','').split(';',1)[0].strip().lower()
                    if kind not in ('application/pdf','application/octet-stream'):
                        raise RegistryFileError('unsupported_file','此工具只支持注册中心的PDF图像文件。')
                    encoding=response.headers.get('content-encoding','identity').strip().lower()
                    if encoding not in ('','identity'):
                        raise RegistryFileError('invalid_response','文件响应编码不符合原件读取契约。')
                    declared=response.headers.get('content-length')
                    if declared is not None:
                        if re.fullmatch(r'[0-9]+',declared) is None:
                            raise RegistryFileError('invalid_response','文件响应长度不合法。')
                        if int(declared)>self.settings.max_download_bytes:
                            raise RegistryFileError('file_too_large','文件超过配置的下载大小上限。')
                    content=bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(content)+len(chunk)>self.settings.max_download_bytes:
                            raise RegistryFileError('file_too_large','文件超过配置的下载大小上限。')
                        content.extend(chunk)
                    if declared is not None and len(content)!=int(declared):
                        raise RegistryFileError('invalid_response','文件响应不完整，请重新读取。')
                    if not content or b'%PDF-' not in content[:1024]:
                        raise RegistryFileError('invalid_file','文件不是可读取的PDF原件。')
                    return bytes(content)
        except RegistryFileError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            raise RegistryFileError('registry_unavailable','注册中心文件暂时无法读取，请稍后重试。') from None

    async def close(self):
        await self._http.aclose()
