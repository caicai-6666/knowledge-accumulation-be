"""终态节点公开消息的后台向量化；不阻塞回答，也不发布检索缓存。"""
import asyncio
import logging
import re
from openai import APIConnectionError
from app.agent.core.prompt.embedding import render_text_embedding_input
from app.config.memory import MemoryEmbeddingSettings
from app.service.embedding import validate_vector

logger = logging.getLogger(__name__)


def _connection_failure_details(error):
    """只提取异常类型与整数错误码，不输出可能含正文、地址或凭据的异常文本。"""
    chain, codes, visited = [], [], set()
    current = error
    while current is not None and id(current) not in visited and len(chain) < 8:
        visited.add(id(current))
        chain.append(type(current).__name__)
        code = getattr(current, 'errno', None)
        if isinstance(current, OSError) and type(code) is int:
            codes.append(str(code))
        else:
            # httpcore 有时仅保留字符串化的 OSError，原始 errno 属性已经丢失。
            # 只提取标准格式内的数字，绝不把整个错误文本写入日志。
            match = re.search(r'\[Errno (-?\d{1,5})\]', str(current))
            if match:
                codes.append(match.group(1))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    return '>'.join(chain), ','.join(codes) or 'none'


def _connection_failure_reason(error):
    """把连接异常映射为固定类别，保留诊断意义而不泄漏原始异常文本。"""
    patterns = (
        ('connection refused', 'connection_refused'),
        ('no route to host', 'no_route_to_host'),
        ('network is unreachable', 'network_unreachable'),
        ('operation not permitted', 'operation_not_permitted'),
        ('permission denied', 'permission_denied'),
        ('nodename nor servname', 'dns_resolution_failed'),
        ('name or service not known', 'dns_resolution_failed'),
        ('getaddrinfo failed', 'dns_resolution_failed'),
        ('certificate verify failed', 'tls_verification_failed'),
    )
    current, visited = error, set()
    while current is not None and id(current) not in visited and len(visited) < 8:
        visited.add(id(current))
        text = str(current).lower()
        for phrase, reason in patterns:
            if phrase in text:
                return reason
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    return 'unknown'


class MemoryEmbeddingService:
    def __init__(self, repository, embeddings, settings: MemoryEmbeddingSettings):
        self.repository, self.embeddings, self.settings = repository, embeddings, settings
        self._task = None
        self._closed = False
        self._cursor = None
        self._connection_diagnosed = False

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
            cause_chain, system_errno = _connection_failure_details(error)
            logger.warning('记忆向量生成失败：error_type=%s cause_chain=%s system_errno=%s connection_reason=%s',
                           type(error).__name__, cause_chain, system_errno,
                           _connection_failure_reason(error))
            try:
                await self.repository.store_memory_embedding(job, None)
            except Exception as saving_error:
                logger.warning('记忆向量失败状态保存失败：error_type=%s', type(saving_error).__name__)
            # 每次服务生命周期只探测一次，避免故障期间每条记忆都创建额外连接。
            if isinstance(error, APIConnectionError) and not self._connection_diagnosed:
                self._connection_diagnosed = True
                try:
                    result = await self.embeddings.diagnose_connection()
                    logger.warning('记忆向量同进程连接诊断：socket=%s socket_errno=%s '
                                   'async_socket=%s async_socket_errno=%s',
                                   result['socket'], result['socket_errno'],
                                   result['async_socket'], result['async_socket_errno'])
                except asyncio.CancelledError:
                    raise
                except Exception as diagnostic_error:
                    logger.warning('记忆向量连接诊断未完成：error_type=%s', type(diagnostic_error).__name__)
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
