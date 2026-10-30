"""内存原件使用前按可信任务用户查询权威 SQL 权限，不依赖网页登录。"""
from app.infrastructure.registry_file import RegistryFileError, validate_resource_id, validate_userid


class RegistryFileAccessService:
    def __init__(self, files):
        # 依赖由组合根共享；本服务不创建连接、不拥有依赖生命周期。
        self.files = files

    async def authorize(self, resource_id, *, userid):
        validate_resource_id(resource_id)
        validate_userid(userid)
        try:
            # read 不使用检索索引或旧候选：在同一 SQL 快照内读取 employees 权限与 resources。
            # 缺平台键、未知等级、员工缺失、资源删除均拒绝；managed_platforms 不参与判断。
            rows = await self.files.read(userid, (resource_id,))
            if not rows:
                raise RegistryFileError('file_forbidden', '文件不存在或当前用户无权读取该文件。')
            if len(rows) != 1 or rows[0].id != resource_id:
                raise ValueError('文件权限查询返回不符合请求')
            # 返回已授权的正式资源记录，展示名称直接使用入库 name，不信任模型或下载头。
            return rows[0]
        except RegistryFileError:
            raise
        except Exception:
            # 网络、数据库和契约故障均失败关闭；不得退回身份快照或绕过校验读取缓存。
            raise RegistryFileError('file_authorization_unavailable', '文件权限校验暂不可用，请稍后重试。') from None
