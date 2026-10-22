"""宿主持有的专家资源；身份按用户和树绑定，不由模型参数指定。"""
from collections import OrderedDict

from app.config.deepseek import load_deepseek_settings
from app.infrastructure.deepseek import DeepSeekClient, DeepSeekRequestError, DeepSeekUnavailableError
from .tool.external_expert import ExternalExpertSessionPool


class ExpertSessions:
    def __init__(self, *, settings=None, client=None, capacity=10, max_scopes=32):
        if any(type(value) is not int or value < 1 for value in (capacity, max_scopes)):
            raise ValueError('专家会话及宿主驻留容量必须为正整数')
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._capacity = capacity
        self._max_scopes = max_scopes
        self._pools = OrderedDict()
        self._closed = False

    def get(self, owner, tree_id):
        if self._closed:
            raise DeepSeekRequestError('专家服务已关闭')
        key = (owner, tree_id)
        pool = self._pools.get(key)
        if pool is None:
            if self._client is None:
                try:
                    settings = self._settings if self._settings is not None else load_deepseek_settings()
                except ValueError:
                    raise DeepSeekRequestError('外部专家配置不合法') from None
                self._client = DeepSeekClient(settings)
            if len(self._pools) >= self._max_scopes:
                victim = next((scope for scope, entry in self._pools.items() if not entry.busy), None)
                if victim is None:
                    raise DeepSeekUnavailableError('专家宿主均在使用中')
                self.release(*victim)
            pool = ExternalExpertSessionPool(tree_id, self._client, capacity=self._capacity)
            self._pools[key] = pool
        self._pools.move_to_end(key)
        return pool

    def release(self, owner, tree_id):
        pool = self._pools.pop((owner, tree_id), None)
        if pool is not None:
            pool.close()

    async def close(self):
        # 调用方先停止生产者；close 也立即撤销迟到响应提交资格。
        self._closed = True
        for pool in self._pools.values():
            pool.close()
        self._pools.clear()
        if self._owns_client and self._client is not None:
            await self._client.close()

