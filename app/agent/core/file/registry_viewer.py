"""远程PDF来源适配；复用图像查看器的页码、渲染、LRU、提交与展示协议。"""
import asyncio
import hashlib
from dataclasses import dataclass
from pydantic import SecretStr
from app.infrastructure.registry_file import RegistryFileError, validate_resource_id
from ..tool.result import ToolPageUnavailable
from .image_viewer import ImageFileViewer, FileViewError
from .cache import FileKey


@dataclass(frozen=True)
class _RegistryReference:
    file_id: str


class RegistryFileViewer(ImageFileViewer):
    def __init__(self, *, scope, client, access_token: SecretStr | None, cache, config=None, authorize=None):
        if access_token is not None and not isinstance(access_token,SecretStr):
            raise TypeError('用户凭据必须由宿主以SecretStr传入')
        self._client = client
        self._token = access_token
        if authorize is not None and not callable(authorize):
            raise TypeError('本地文件鉴权必须是可调用适配器')
        self._authorize = authorize
        super().__init__(scope=scope,read_file=self._read_remote,authorize=self._reference,
            cache=cache,config=config)

    async def file_name(self, file_id):
        """仅返回当前用户可读资源的入库 name，不从旧候选或下载响应推断名称。"""
        self._require_open()
        file_id=validate_resource_id(file_id)
        if self._authorize is None:
            return None
        record=await self._authorize(file_id,self._token)
        self._require_open()
        if record is None:
            return None  # 内部宿主若仅提供许可校验，保留通用动作提示。
        if getattr(record,'id',None)!=file_id:
            raise RegistryFileError('file_authorization_unavailable','文件权限校验暂不可用，请稍后重试。')
        return record.name if isinstance(record.name,str) else None

    async def _reference(self, file_id):
        self._require_open()
        file_id = validate_resource_id(file_id)
        if self._authorize is not None:
            await self._authorize(file_id, self._token)
        # 未装配本平台权威鉴权的内部宿主仍须每次 GET，不能退化为无条件缓存命中。
        return _RegistryReference(file_id)

    async def _read_remote(self, file_id):
        content=await self._client.read_file(file_id,self._token)
        return await self._reference(file_id),content

    async def _source(self, file_id):
        if self._authorize is not None:
            reference = await self._reference(file_id)
            # 正式资源 ID 对应不可变原件；替换原件会登记新资源 ID，权限等级仍实时查 SQL。
            return reference, None, FileKey(self.scope, file_id)
        reference,content=await self.read_file(file_id)
        self._require_open()
        if not isinstance(content,bytes):
            raise RegistryFileError('invalid_file','注册中心未返回有效文件字节。')
        digest=await asyncio.to_thread(lambda:hashlib.sha256(content).hexdigest())
        # 正式资源ID是整数，上传附件ID是UUID；同一用户/树的公共LRU不会混淆两种来源。
        return reference,content,FileKey(self.scope,file_id,digest)

    def _validate_file_id(self, file_id):
        return validate_resource_id(file_id)

    def _page_description(self, file_id, target, count):
        return f'文件来源: 注册中心\n文件类型: 多图\n文件ID: {file_id}\n页码: {target}\n总页数: {count}'

    @staticmethod
    def _error(error):
        if isinstance(error,RegistryFileError):
            return FileViewError(error.code,str(error))
        mapped=ImageFileViewer._error(error)
        return FileViewError(mapped.code,str(mapped).replace('附件','注册中心文件'))

    async def resolve_page(self, reference):
        try:
            return await super().resolve_page(reference)
        except FileViewError:
            # 已打开页面仍须重新鉴权；失败只标记资料不可用，不能回放缓存图像。
            raise ToolPageUnavailable('注册中心页面当前不可读取，请重新登录或核对访问权限后查看。') from None

    async def aclose(self):
        try:
            await super().aclose()
        finally:
            self._token=None
