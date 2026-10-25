"""权威文件快照→内存检索→权限复核，不落盘派生索引或发布陈旧候选。"""
import asyncio
import hashlib
import json
import math
from app.agent.core.prompt.embedding import render_text_embedding_input, TEXT_EMBEDDING_INSTRUCTION
from app.config.file_retrieval import FileRetrievalSettings
from app.repository.registered_file import RegisteredFileRepository
from app.infrastructure.file_search_index import MemoryFileSearchIndex
from app.service.embedding import validate_vector


class FileRetrievalService:
    def __init__(self, embeddings, *, source=None, index=None, settings=None):
        self.source = source if source is not None else RegisteredFileRepository()
        self.embeddings = embeddings
        self.settings = settings or FileRetrievalSettings()
        self.index = index if index is not None else MemoryFileSearchIndex(max_vectors=self.settings.cache_max_vectors)
        self.closed = False
        self._lock = asyncio.Lock()

    async def read(self, userid, ids=None, platforms=None):
        if self.closed:
            raise RuntimeError('文件检索已关闭')
        rows = await self.source.read(userid, ids, platforms)
        if self.closed:
            raise RuntimeError('文件检索已关闭')
        return rows

    def _key(self, row):
        # 源向量没有编码来源，不能直接混用。缓存按正文与完整编码契约寻址，可安全重建。
        model = self.embeddings.settings if self.embeddings is not None else None
        payload = [row.id,row.platform,row.name,row.abstract,TEXT_EMBEDDING_INSTRUCTION,
                   model.model if model else None, model.dimensions if model else None,
                   str(model.base_url) if model else None, model.normalize if model else None]
        return hashlib.sha256(json.dumps(payload,ensure_ascii=False).encode()).hexdigest()

    async def search(self, userid, field, query, ids=None, platforms=None):
        if self.closed:
            raise RuntimeError('文件检索已关闭')
        if field not in ('name','abstract','passport') or not isinstance(query,str) or not query.strip():
            raise ValueError('文件查询参数不合法')
        if field == 'passport':
            if len(query)>512:
                raise ValueError('通行证最多512字符')
            # 精确登记字段筛选不调用模型/向量，不以相关性 Top-K 截断同通行证的其他文件。
            rows = await self.source.read(userid,ids,platforms,passport=query)
            if self.closed:
                raise RuntimeError('文件检索已关闭')
            if not rows:
                return ()
            latest = await self.source.read(userid,tuple(row.id for row in rows),platforms,passport=query)
            if self.closed or {r.id:r for r in rows}!={r.id:r for r in latest}:
                raise ValueError('文件资料、通行证或权限已变化，请重新检索')
            return tuple((row.id,1.) for row in rows)
        rows = await self.read(userid, ids, platforms)
        rows = tuple(r for r in rows if ((r.name if field=='name' else r.abstract) or '').strip())
        if not rows:
            return ()
        by_key = {self._key(row):row for row in rows}
        vector = None
        async with self._lock:
            if self.closed:
                raise RuntimeError('文件检索已关闭')
            try:
                await self.index.ensure([(key,row.name or '',row.abstract or '') for key,row in by_key.items()])
                if field=='abstract':
                    if self.embeddings is None:
                        raise RuntimeError('摘要混合查询需要配置Embedding')
                    missing = await self.index.missing_vectors(tuple(by_key))
                    if not missing <= set(by_key):
                        raise ValueError('索引返回越界条目')
                    keys = sorted(missing)
                    # 复用现有模型批量、并发与超时，不为旧向量做未经验证的兼容假设。
                    batch = self.embeddings.settings.batch_size
                    for offset in range(0,len(keys),batch):
                        selected = keys[offset:offset+batch]
                        vectors = await self.embeddings.embed_texts([render_text_embedding_input(by_key[k].abstract) for k in selected])
                        if len(vectors)!=len(selected):
                            raise ValueError('向量数量不符')
                        for value in vectors:
                            validate_vector(value,self.embeddings.settings.dimensions)
                        await self.index.save_vectors(list(zip(selected,vectors)))
                    vector = await self.embeddings.embed_text(render_text_embedding_input(query))
                    validate_vector(vector,self.embeddings.settings.dimensions)
                lanes = await self.index.rank(tuple(by_key),query,field,vector,
                    self.settings.top_k*self.settings.candidate_multiplier)
            finally:
                # 正常、失败和取消均释放本次正文/词频/在用向量，只留下有界向量LRU。
                self.index.clear_documents()
        scores = {}
        for lane in lanes:
            if len({k for k,_ in lane})!=len(lane):
                raise ValueError('召回重复条目')
            for rank,(key,score) in enumerate(lane,1):
                if key not in by_key or not math.isfinite(score):
                    raise ValueError('召回越界或分数非法')
                scores[key] = scores.get(key,0)+1/(self.settings.rrf_k+rank) if field=='abstract' else score
        ranked = sorted(scores,key=lambda k:(-scores[k],int(by_key[k].id)))[:self.settings.top_k]
        if not ranked:
            return ()
        latest = {r.id:r for r in await self.read(userid,tuple(by_key[k].id for k in ranked),platforms)}
        # 权限撤销、删除、改名或摘要更新时，不将旧快照作为成功结果发布。
        if any(latest.get(by_key[k].id)!=by_key[k] for k in ranked):
            raise ValueError('文件资料或权限已变化，请重新检索')
        return tuple((by_key[k].id,scores[k]) for k in ranked)

    async def close(self):
        self.closed = True
        async with self._lock:
            try:
                await self.source.close()
            finally:
                self.index.close()
