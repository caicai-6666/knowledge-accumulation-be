"""到期提醒生产者；独立于正在执行的助理任务，只入队，不直接通知用户。"""
import asyncio
import logging
from app.model.question_tree import utc_now

logger = logging.getLogger(__name__)


class AssistantReminderScheduler:
    def __init__(self, settings, repository, delivery, *, clock=utc_now):
        self.settings, self.repository, self.delivery, self.clock = settings, repository, delivery, clock
        self._task = None
        self._closed = False

    async def tick(self):
        now = self.clock()
        await self.repository.materialize_due(now=now, limit=self.settings.reminder_batch_size)
        items = await self.repository.pending_deliveries(now=now, limit=self.settings.reminder_batch_size,
            retry_window_seconds=self.settings.publication_retry_window_seconds)
        for item in items:
            await self.delivery.deliver(item, retry_after_seconds=self.settings.reminder_retry_seconds)

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('提醒调度未完成，将重试：error_type=%s', type(error).__name__)
            await asyncio.sleep(self.settings.reminder_poll_seconds)

    async def start(self):
        if self._closed:
            raise RuntimeError('提醒调度器已关闭')
        if self.settings.enabled and self._task is None:
            await self.repository.validate_schema()
            self._task = asyncio.create_task(self._run())

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
