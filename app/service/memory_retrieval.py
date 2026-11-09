"""祖先任务的字段混合召回；身份与范围由宿主绑定，模型不能提供节点集合。"""
import asyncio
import asyncmy
from llama_index.core.schema import TextNode, NodeWithScore
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.llms import MockLLM
from app.agent.core.prompt.embedding import render_text_embedding_input
from app.agent.core.subgraph.memory_retrieval.schema import RankedTask, RetrievalPlan
from app.infrastructure.myvector import MyVectorStore
from app.repository.memory_retrieval import MemoryWindowError, read_memory_tasks

KINDS = {'user_input': 'question', 'intermediate_output': 'intermediate', 'final_output': 'final'}


def eligible_ancestors(snapshot, current_node_id, plan):
    return tuple(node for node in snapshot.ancestor_tasks(current_node_id)
        if node.status in ('complete','cancelled','superseded','failed')
        and (plan.statuses is None or node.status in plan.statuses)
        and (plan.created_from is None or int(node.created_at.timestamp()*1000) >= plan.created_from)
        and (plan.created_before is None or int(node.created_at.timestamp()*1000) < plan.created_before))


class _RankedRoute(BaseRetriever):
    def __init__(self, route):
        super().__init__()
        self.route = route

    def _retrieve(self, query_bundle):
        raise RuntimeError('请使用异步召回')

    async def _aretrieve(self, query_bundle):
        # 各路同一任务使用相同内容hash，沿用参考实现的去重身份。
        return [NodeWithScore(node=TextNode(text=identifier, id_=identifier), score=score)
                for identifier, score in self.route]


async def fuse_routes(routes, ancestors, query, top_k=20):
    by_id = {node.id: node for node in ancestors}
    for route in routes:
        ids = [identifier for identifier, _ in route]
        if len(ids) > top_k or len(set(ids)) != len(ids) or any(i not in by_id for i in ids):
            raise ValueError('召回候选越界或重复')
    if not routes:
        return ()
    # 与参考项目使用同一融合器；num_queries=1 禁止模型扩写，MockLLM不会被调用。
    fusion = QueryFusionRetriever([_RankedRoute(route) for route in routes], llm=MockLLM(),
        num_queries=1, mode='reciprocal_rerank', similarity_top_k=top_k, use_async=True)
    hits = await fusion.aretrieve(query)
    hits.sort(key=lambda hit: (-hit.score, by_id[hit.node.node_id].sequence, hit.node.node_id))
    return tuple(RankedTask(node_id=hit.node.node_id, score=hit.score) for hit in hits)


class AncestorMemoryRetrieval:
    def __init__(self, repository, embeddings, *, store=None):
        self.repository, self.embeddings, self.store = repository, embeddings, store
        self._pool = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _get_store(self):
        if self._closed:
            raise RuntimeError('记忆召回服务已关闭')
        if self.store is None:
            await self.repository._initialize()
            settings = self.repository._settings
            async with asyncio.timeout(settings.operation_timeout_seconds):
                self._pool = await asyncmy.create_pool(host=settings.host, port=settings.port,
                    user=settings.user, password=settings.password.get_secret_value(), db=settings.database,
                    charset='utf8mb4', autocommit=True, minsize=1, maxsize=2,
                    connect_timeout=settings.connect_timeout_seconds, pool_recycle=300)
            self.store = MyVectorStore(self._pool, dimensions=self.embeddings.settings.dimensions, search_mode='exact')
        return self.store

    async def search(self, tree_id, owner, current_node_id, plan):
        plan = RetrievalPlan.model_validate(plan)
        if self._closed:
            raise RuntimeError('记忆召回服务已关闭')
        snapshot = await self.repository.load_tree(tree_id, owner)
        ancestors = eligible_ancestors(snapshot, current_node_id, plan)
        if not ancestors:
            return ()
        all_documents = await self.repository.load_searchable_memory_documents(tree_id, owner)
        allowed = {node.id: node for node in ancestors}
        documents = {d.memory.id: d for d in all_documents if d.memory.node_id in allowed and d.text.strip()
            and (allowed[d.memory.node_id].status != 'cancelled' or d.memory.kind == 'question')}
        queries = [q for q in plan.queries if any(d.memory.kind == KINDS[q.field] for d in documents.values())]
        if not queries:
            return ()
        if self.embeddings is None:
            raise RuntimeError('未配置记忆向量模型')
        async with asyncio.TaskGroup() as group:
            pending = [group.create_task(self.embeddings.embed_text(render_text_embedding_input(q.query))) for q in queries]
        encoded = [(KINDS[q.field], q.query, task.result()) for q, task in zip(queries, pending)]
        # 统一刷新整树权威缓存，排名严格限定祖先；不会把缓存改成某条分支的局部副本。
        nodes = [TextNode(id_=d.memory.id, text=d.text, embedding=list(d.memory.embedding), metadata={
            'owner_userid':owner,'tree_id':tree_id,'node_id':d.memory.node_id,'kind':d.memory.kind})
            for d in all_documents if d.text.strip()]
        async with self._lock:
            store = await self._get_store()
            async with self.repository.memory_index_write_scope(tree_id, owner):
                routes = await store.rank_memory_snapshot(nodes, owner_userid=owner, tree_id=tree_id,
                    node_ids=tuple(allowed), queries=encoded)
        ranked = await fuse_routes(routes, ancestors, plan.queries[0].query)
        # 返回前重新校验范围与命中来源。删除或改变来源不能继续发布陈旧候选。
        latest = await self.repository.load_tree(tree_id, owner)
        current = {node.id: node for node in eligible_ancestors(latest, current_node_id, plan)}
        if any(current.get(item.node_id) != allowed[item.node_id] for item in ranked):
            raise ValueError('记忆候选已变化')
        selected = {item.node_id for item in ranked}
        refreshed = {d.memory.id:d for d in await self.repository.load_searchable_memory_documents(tree_id, owner)}
        if any(refreshed.get(i) != doc for i, doc in documents.items() if doc.memory.node_id in selected):
            raise ValueError('记忆来源已变化')
        if self._closed:
            raise RuntimeError('记忆召回服务已关闭')
        return ranked

    async def read(self, tree_id, owner, current_node_id, ids):
        if self._closed:
            raise RuntimeError('记忆召回服务已关闭')
        return await read_memory_tasks(self.repository, tree_id, owner, current_node_id, ids)

    async def window(self, tree_id, owner, current_node_id, *, task_id, before=0, after=0):
        """沿完整祖先链定位相邻任务；不依赖助理压缩窗口、向量状态或时间排序。"""
        if self._closed:
            raise RuntimeError('记忆召回服务已关闭')
        if (type(before) is not int or type(after) is not int or before < 0 or after < 0
                or not 1 <= before + after <= 20):
            raise MemoryWindowError('任务窗口数量不合法，尚未建立结果集。',
                'before 和 after 必须为非负整数，合计为 1 至 20；不包含锚点任务本身。')
        snapshot = await self.repository.load_tree(tree_id, owner)
        if snapshot.tree.kind != 'assistant':
            raise MemoryWindowError('任务窗口查找仅适用于个人助理，本次未建立结果集。',
                '请使用 search_memory 查找当前会话的历史任务。')
        tasks = snapshot.ancestor_tasks(current_node_id)
        anchor = next((index for index, node in enumerate(tasks) if node.id == task_id), None)
        if anchor is None:
            # 不区分不存在与越权，避免通过报错探测其他用户或分支的任务。
            raise MemoryWindowError('该任务 ID 不在当前可读取的历史任务范围内，本次未建立结果集。',
                '请核对并改用已知正确的历史任务 ID，原样填写 task_id；已有正确 ID 时可直接重试，无须先搜索。没有可用 ID 时，可通过 search_memory 和 view_memory_query 定位；不能使用查询 ID 或记录序号。')
        selected = tasks[max(0, anchor-before):anchor] + tasks[anchor+1:anchor+1+after]
        ids = tuple(node.id for node in selected)
        # 发布前确认原文可读，翻页时还会回查归属与范围；读取失败不能伪装为空结果。
        if ids:
            await self.read(tree_id, owner, current_node_id, ids)
        return ids

    async def close(self):
        async with self._lock:
            self._closed = True
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None
