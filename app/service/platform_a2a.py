"""共享反馈调度：持久关联、广播缓冲、单聊分发和提交后的续聊授权。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import logging
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.infrastructure.platform_a2a import PlatformA2AError
from app.model.platform_a2a import A2APublish, A2AReply
from app.model.question_tree import utc_now

logger = logging.getLogger(__name__)


class PlatformA2AService:
    def __init__(self, repository, client, directory, settings, *, clock=utc_now):
        self.repository, self.client, self.directory = repository, client, directory
        self.settings, self.clock = settings, clock
        self._task = None
        self._closed = False
        self._events = {}
        self._locks = {}
        self._contexts = {}
        self._tick_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.max_concurrent_requests)
        self._pull_key = None
        self._pull_renewals = ()
        self._deliveries = {}

    @asynccontextmanager
    async def _lock(self, pool, key):
        entry = pool.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                pool.pop(key, None)

    async def start(self):
        if self._closed:
            raise RuntimeError('平台协作服务已关闭')
        if self._task is None:
            await self.repository.validate_schema()
            self._task = asyncio.create_task(self._run())

    async def close(self):
        self._closed = True
        for event in self._events.values():
            event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        # 持久请求与已保存回复保留；服务关闭不假装已完成，也不删除其他调用的等待状态。

    async def invoke(self, *, userid, tree_id, node_id, call_id, message, platform_codes=None,
                     response_id=None, platform_code=None):
        if self._closed or self._task is None:
            raise PlatformA2AError('unavailable', status=503)
        if not call_id:
            raise ValueError('平台工具必须绑定可信调用标识')
        targets = sorted(platform_codes or [platform_code])
        rows = await self.directory.list_platforms()
        valid = {p.code for p in rows if p.code != 'knowledge-accumulation'
                 and p.supports_a2a and p.is_sub_management_platform}
        if not set(targets) <= valid:
            raise ValueError('目标不是支持协作的子管理平台')
        identity = uuid5(NAMESPACE_URL, f'platform-a2a:{userid}:{tree_id}:{node_id}:{call_id}').hex
        request = A2APublish(broadcast_id=identity, userid=userid, message=message,
            target_platform_codes=targets if response_id is None else None,
            response_id=response_id, response_platform_code=platform_code if response_id is not None else None)
        # 同一个历史上下文的续聊串行；独立话题/用户/平台可并发，不让无关联调用互相阻塞。
        context_key = (userid, tree_id, platform_code, response_id) if response_id is not None else ('new', identity)
        async with self._lock(self._contexts, context_key):
            if response_id is not None and not await self.repository.has_context(userid, tree_id, platform_code, response_id):
                raise ValueError('该平台响应不属于当前用户和会话已接受的结果，或不可续聊')
            event = self._events.setdefault(identity, asyncio.Event())
            now = self.clock()
            try:
                await self.repository.create(request=request, tree_id=tree_id, node_id=node_id,
                    targets=targets, now=now, deadline=now+timedelta(seconds=self.settings.wait_seconds))
                while not self._closed:
                    call = await self.repository.get(identity)
                    if call['result_json'] is not None:
                        return identity, call['result_json']
                    event.clear()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=self.settings.poll_seconds)
                    except TimeoutError:
                        pass
                raise PlatformA2AError('unavailable', status=503, uncertain=True)
            except asyncio.CancelledError:
                # 取消工具不等于取消远端执行；冻结本地结果并安排关闭广播/清理迟到反馈。
                await asyncio.shield(self._abandon(identity))
                raise
            finally:
                if self._events.get(identity) is event:
                    self._events.pop(identity, None)

    async def _abandon(self, identity):
        async with self._lock(self._locks, identity):
            call = await self.repository.get(identity)
            if call and call['state'] in ('pending', 'waiting'):
                await self.repository.update(identity, state='abandoned', finished_at=self.clock(),
                    result_json={'status': 'cancelled', 'replies': []},
                    close_pending=call['request_json'].get('response_id') is None)

    async def _finish(self, call, *, error=None, uncertain=False):
        published = call['published'] is not None
        replies = [call['replies'].get(code) or dict(platform_code=code, status='timeout',
            message='本轮等待结束前未收到回复，不能据此判断对方未收到请求。', response_id=None)
            for code in call['targets']]
        result = {'status': ('publish_unknown' if uncertain else 'publish_failed') if error else 'completed',
                  'replies': replies}
        await self.repository.update(call['broadcast_id'], state='completed', result_json=result,
            finished_at=self.clock(), close_pending=call['request_json'].get('response_id') is None
            and (published or uncertain))
        if event := self._events.get(call['broadcast_id']):
            event.set()

    def _validate_published(self, call, result):
        expected_flow = 'direct' if call['request_json'].get('response_id') is not None else 'broadcast'
        if (result.broadcast_id != call['broadcast_id'] or result.userid != call['userid']
                or result.target_platform_codes != call['targets'] or result.reply_flow != expected_flow
                or sorted(t.target_platform_code for t in result.tasks) != call['targets']
                or len({t.task_id for t in result.tasks}) != len(call['targets'])):
            raise PlatformA2AError('publish', uncertain=True)

    async def _advance(self, identity):
        async with self._slots, self._lock(self._locks, identity):
            call = await self.repository.get(identity)
            if call is None:
                return
            if call['close_pending']:
                try:
                    await self.client.close_broadcast(identity)
                except PlatformA2AError as error:
                    if error.status != 404:
                        raise
                await self.repository.update(identity, close_pending=False)
                return
            if call['state'] not in ('pending', 'waiting'):
                return
            if self.clock() >= call['deadline_at']:
                await self._finish(call, error=call['published'] is None, uncertain=call['published'] is None)
                return
            if call['state'] == 'pending':
                try:
                    published = await self.client.publish(A2APublish.model_validate(call['request_json']))
                    self._validate_published(call, published)
                except PlatformA2AError as error:
                    if error.status in (403, 409, 422):
                        await self._finish(call, error=True)
                        return
                    raise
                await self.repository.update(identity, state='waiting', published=published.model_dump(mode='json'))
                call = await self.repository.get(identity)
            if call['published']['reply_flow'] == 'broadcast':
                # 发布后先留出固定响应缓冲，再收集所有平台终态；首条回复不会提前关闭广播。
                from datetime import datetime
                published_at = datetime.fromisoformat(call['published']['created_at'].replace('Z', '+00:00'))
                if self.clock() < published_at+timedelta(seconds=self.settings.response_buffer_seconds):
                    return
                package = await self.client.read(identity)
                if (package.broadcast_id != identity or package.userid != call['userid']
                        or sorted(package.target_platform_codes) != call['targets']):
                    raise ValueError('广播反馈归属不匹配')
                if package.state == 'closed':
                    await self._finish(call, error=True)
                    return
                codes = [reply.platform_code for reply in package.replies]
                if len(set(codes)) != len(codes) or set(package.pending_platform_codes) != set(call['targets'])-set(codes):
                    raise ValueError('广播反馈集合不一致')
                for reply in package.replies:
                    await self.repository.add_reply(identity, reply)
            call = await self.repository.get(identity)
            if set(call['replies']) == set(call['targets']):
                await self._finish(call)

    async def _direct(self):
        # 单一接收器服务所有用户和模型调用；反馈按原请求定位，不按到达顺序分给等待者。
        await self._drain_deliveries()
        if self._pull_key is None:
            self._pull_key = uuid4().hex
            # 同一次网络重试冻结续租参数，不能复用幂等键却改掉其中的凭证或列表。
            self._pull_renewals = tuple(d for d in self._deliveries.values() if d.lease_expires_at > self.clock())
        try:
            window = await self.client.pull(self._pull_key, self._pull_renewals)
        except PlatformA2AError as error:
            if error.status == 409:
                self._pull_key = None
            raise
        for delivery in [*window.messages, *window.renewed_messages]:
            self._deliveries[delivery.message.message_id] = delivery
        self._pull_key = None
        self._pull_renewals = ()
        await self._drain_deliveries()

    async def _drain_deliveries(self):
        async def consume(key, delivery):
            async with self._slots:
                await self._consume_delivery(key, delivery)
        results = await asyncio.gather(*(consume(k, d) for k, d in tuple(self._deliveries.items())),
                                       return_exceptions=True)
        for error in results:
            if isinstance(error, asyncio.CancelledError):
                raise error
            if isinstance(error, Exception):
                self._log(error)

    async def _consume_delivery(self, key, delivery):
        if self.clock() >= delivery.lease_expires_at:
            self._deliveries.pop(key, None)
            return
        reply = delivery.message
        # 短事务可靠保存完成后才 ack；崩溃未确认的反馈由中间件租约重领，重复保存须完全一致。
        if not await self.repository.add_reply(reply.broadcast_id, reply):
            return
        try:
            await self.client.acknowledge(delivery)
        except PlatformA2AError as error:
            if error.status == 409:
                self._deliveries.pop(key, None)
            raise
        self._deliveries.pop(key, None)
        if event := self._events.get(reply.broadcast_id):
            event.set()


    async def tick(self):
        if self._closed:
            return
        async with self._tick_lock:
            rows = await self.repository.pending()
            results = await asyncio.gather(*(self._advance(row['broadcast_id']) for row in rows), return_exceptions=True)
            for error in results:
                if isinstance(error, BaseException):
                    if isinstance(error, asyncio.CancelledError):
                        raise error
                    self._log(error)
            try:
                await self._direct()
            except Exception as error:
                self._log(error)

    @staticmethod
    def _log(error):
        logger.warning('平台协作等待重试：error_type=%s operation=%s status=%s uncertain=%s',
            type(error).__name__, getattr(error, 'operation', None), getattr(error, 'status', None),
            getattr(error, 'uncertain', None))

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except Exception as error:
                self._log(error)
            await asyncio.sleep(self.settings.poll_seconds)
