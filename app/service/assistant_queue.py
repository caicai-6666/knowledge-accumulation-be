"""队列会话编排与单次消费；任务处理器负责持久化/交付，确认不代替发送结果。"""
import asyncio
from contextlib import asynccontextmanager
import logging

from app.infrastructure.assistant_queue import AssistantQueueClient, AssistantQueueError
from app.model.assistant_queue import AssistantDeliveryLease
from app.model.question_tree import utc_now
from app.service.middleware_session import MiddlewareSessionService

logger = logging.getLogger(__name__)


class AssistantLeaseLost(AssistantQueueError):
    def __init__(self):
        super().__init__('lease', status=409)


class AssistantTaskNotCompleted(RuntimeError):
    def __init__(self):
        super().__init__('助理任务尚未确认可靠处理完成')


class AssistantQueueService:
    def __init__(self, sessions: MiddlewareSessionService, client: AssistantQueueClient,
                 *, platform_code: str, clock=utc_now):
        self.sessions, self.client, self.platform_code = sessions, client, platform_code
        self._clock = clock
        self._locks = {}
        self._active = set()
        self._closed = False

    async def _call(self, operation, invoke):
        if self._closed:
            raise RuntimeError('助理队列服务已关闭')
        token = self.sessions.get_access_token()
        if token is None:
            raise AssistantQueueError(operation, status=503)
        try:
            return await invoke(token)
        except AssistantQueueError as error:
            if error.status == 401:
                self.sessions.invalidate_access_token(token)
            logger.warning('助理队列操作失败：operation=%s status=%s uncertain=%s',
                           operation, error.status, error.uncertain)
            raise

    async def publish(self, request, *, uploads=()):
        return await self._call('publish',lambda token:self.client.publish(request,token,
            platform_code=self.platform_code,uploads=uploads))

    async def pull(self, userid):
        return await self._call('pull',lambda token:self.client.pull(userid,token))

    async def acknowledge(self, lease: AssistantDeliveryLease):
        # 相同 ack 可以在首次确认成功、附件清理失败或回执丢失后重试，即使原租约已过期。
        return await self._call('ack',lambda token:self.client.acknowledge(lease,token))

    async def status(self, userid):
        return await self._call('status',lambda token:self.client.status(userid,token))

    async def download(self, lease, reference):
        if reference not in lease.task.files:
            raise ValueError('附件不属于本次领取任务')
        return await self._call('download',lambda token:self.client.download(reference,token))

    async def renew(self, lease):
        if lease.lease_expires_at <= self._clock():
            raise AssistantLeaseLost()
        current = await self.pull(lease.task.userid)
        # 重领的新 token 或另一任务都不是续租成功，绝不能换绑正在执行的任务。
        if (current is None or current.task != lease.task or current.delivery_token != lease.delivery_token
                or current.lease_expires_at <= self._clock()):
            raise AssistantLeaseLost()
        return current

    @asynccontextmanager
    async def _user_lock(self, userid):
        entry = self._locks.setdefault(userid, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self._locks[userid]

    async def execute_once(self, userid, handle):
        """最多处理一次领取；handle(lease) 必须在持久化和结果可靠交付后返回 True。"""
        if self._closed:
            raise RuntimeError('助理队列服务已关闭')
        current = asyncio.current_task()
        if current in self._active:
            raise RuntimeError('任务处理器不能递归消费队列')
        self._active.add(current)
        try:
            async with self._user_lock(userid):
                lease = await self.pull(userid)
                if lease is None:
                    return None
                if lease.lease_expires_at <= self._clock():
                    raise AssistantLeaseLost()
                latest = [lease]

                async def keep_lease():
                    while True:
                        remaining = (latest[0].lease_expires_at-self._clock()).total_seconds()
                        if remaining <= 0:
                            raise AssistantLeaseLost()
                        await asyncio.sleep(remaining / 2)
                        remaining = (latest[0].lease_expires_at-self._clock()).total_seconds()
                        if remaining <= 0:
                            raise AssistantLeaseLost()
                        async with asyncio.timeout(remaining):
                            latest[0] = await self.renew(latest[0])

                work = asyncio.create_task(handle(lease))
                renewal = asyncio.create_task(keep_lease())
                try:
                    done, _ = await asyncio.wait({work,renewal}, return_when=asyncio.FIRST_COMPLETED)
                    if renewal in done:
                        # 租约或平台依赖不可用时停止处理，不继续执行已失去所有权的任务。
                        await renewal
                        raise AssistantLeaseLost()
                    if await work is not True:
                        raise AssistantTaskNotCompleted()
                    renewal.cancel()
                    await asyncio.gather(renewal,return_exceptions=True)
                    if latest[0].lease_expires_at <= self._clock():
                        raise AssistantLeaseLost()
                    return await self.acknowledge(latest[0])
                finally:
                    for task in (work,renewal):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(work,renewal,return_exceptions=True)
        finally:
            self._active.discard(current)

    async def close(self):
        self._closed = True
        tasks = [task for task in self._active if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        # 平台会话和 HTTP 客户端均由组合根注入，关闭本服务不破坏其他使用方。
