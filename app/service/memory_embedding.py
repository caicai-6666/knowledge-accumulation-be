"""终态节点公开消息的后台向量化；不阻塞回答，也不发布检索缓存。"""
import asyncio
import logging
from app.agent.core.prompt.embedding import render_text_embedding_input
from app.config.memory import MemoryEmbeddingSettings
from app.service.embedding import validate_vector

logger = logging.getLogger(__name__)


class MemoryEmbeddingService:
    def __init__(self, repository, embeddings, settings: MemoryEmbeddingSettings):
        self.repository, self.embeddings, self.settings = repository, embeddings, settings
        self._task = None
        self._closed = False
        self._cursor = None

    async def start(self):
        if self._closed:
            raise RuntimeError('记忆向量化服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='memory-embedding')

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _process(self, job):
        try:
            vector = await self.embeddings.embed_text(render_text_embedding_input(job.text))
            validate_vector(vector, self.embeddings.settings.dimensions)
        except asyncio.CancelledError:
            # pending/failed 状态保持可恢复；取消不是一次确定的模型失败。
            raise
        except Exception as error:
            logger.warning('记忆向量生成失败：error_type=%s', type(error).__name__)
            try:
                await self.repository.store_memory_embedding(job, None)
            except Exception as saving_error:
                logger.warning('记忆向量失败状态保存失败：error_type=%s', type(saving_error).__name__)
            return
        try:
            # 网络请求不持数据库锁；提交前重新确认归属、终态、原文及成功状态。
            await self.repository.store_memory_embedding(job, vector)
        except Exception as error:
            logger.warning('记忆向量保存失败：error_type=%s', type(error).__name__)

    async def process_batch(self):
        jobs = await self.repository.list_memory_embedding_jobs(
            after_id=self._cursor, limit=self.settings.batch_size)
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(self._process(job))
        # 按 ID 游标走完整轮，失败项不能一直占据首页，挤掉其他待处理消息。
        self._cursor = jobs[-1].memory.id if len(jobs) == self.settings.batch_size else None
        return len(jobs)

    async def _run(self):
        while not self._closed:
            try:
                count = await self.process_batch()
                if count == self.settings.batch_size:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('记忆向量扫描失败：error_type=%s', type(error).__name__)
            await asyncio.sleep(self.settings.scan_interval_seconds)
