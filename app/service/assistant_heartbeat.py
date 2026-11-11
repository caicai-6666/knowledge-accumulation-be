"""空闲心跳生产者：中央状态决定时机，同一确认周期使用稳定发布键。"""
import asyncio
import hashlib
import json
import logging
from datetime import timedelta, timezone

from app.model.assistant_queue import PublishAssistantTask
from app.model.question_tree import utc_now

logger = logging.getLogger(__name__)

# 同一请求键的正文必须保持一致，重试、重启或修改间隔不能改变已发布请求。
HEARTBEAT_MESSAGE = '请结合已有记忆、工作区和待办，检查现在是否有值得推进、核查或整理的事项。'


def heartbeat_request(userid, last_heartbeat_at):
    anchor = last_heartbeat_at.astimezone(timezone.utc).isoformat(timespec='microseconds') if last_heartbeat_at else None
    key = hashlib.sha256(json.dumps([userid, anchor], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return PublishAssistantTask(userid=userid, task_type='heartbeat', message=HEARTBEAT_MESSAGE,
                                request_id='assistant-heartbeat:'+key)


class AssistantHeartbeatScheduler:
    def __init__(self, settings, repository, employees, queue, *, clock=utc_now):
        self.settings, self.repository, self.employees, self.queue = settings, repository, employees, queue
        self.clock = clock
        self._task = None
        self._closed = False
        self._tick_lock = asyncio.Lock()

    async def _check_user(self, userid):
        # 与消费者共用跨实例用户锁。正在生成/交付时不等待、不抢占，也不插入心跳。
        async with self.repository.user_scope(userid) as acquired:
            if not acquired or self._closed:
                return
            assistant = await self.repository.heartbeat_candidate(userid)
            if assistant is None or await self.employees.find_by_userid(userid) is None:
                return
            status = await self.queue.status(userid)
            if status.has_in_progress_task:
                return
            now = self.clock()
            last = status.last_heartbeat_at
            interval = self.settings.heartbeat_interval_seconds
            if interval < 0:
                return
            if last is not None and now < last + timedelta(seconds=interval):
                return
            if not self._closed:
                # status 不展示待领取心跳；相同确认时间始终生成同一个键，由中央幂等合并重试。
                # 不以发布成功更新 last_heartbeat_at，不直接调用模型或钉钉。
                await self.queue.publish(heartbeat_request(userid, last))

    async def tick(self):
        if self._closed or not self.settings.enabled or self.settings.heartbeat_interval_seconds < 0:
            return
        async with self._tick_lock:
            if self._closed:
                return
            users = await self.repository.heartbeat_candidates()
            for userid in users:
                if self._closed:
                    return
                try:
                    # 命名锁连接失效会取消持锁协程；隔离为子任务，避免停掉整个扫描器。
                    await asyncio.create_task(self._check_user(userid))
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    logger.warning('助理心跳检查已取消，将在后续扫描重试')
                except Exception as error:
                    # 不回显用户、请求正文、SQL 或访问凭据；一个用户故障不影响后续用户。
                    logger.warning('助理心跳检查未完成，将重试：error_type=%s', type(error).__name__)

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('助理心跳扫描未完成，将重试：error_type=%s', type(error).__name__)
            await asyncio.sleep(self.settings.heartbeat_poll_seconds)

    async def start(self):
        if self._closed:
            raise RuntimeError('心跳调度器已关闭')
        if self.settings.enabled and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
