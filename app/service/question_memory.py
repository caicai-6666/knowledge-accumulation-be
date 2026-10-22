"""共用原文/向量缓存，提供关键词及语义查询，返回前校验权威来源。"""

import asyncio
import math

from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.types import MetadataFilter, MetadataFilters, VectorStoreQuery
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.myvector import MemoryIndexMetadata, MyVectorStore
from app.model.question_tree import TreeID
from app.repository.question_tree import MySQLQuestionTreeRepository
from app.service.embedding import validate_vector


class MemoryMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    memory_id: TreeID
    node_id: TreeID
    source_id: str
    kind: str
    similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)
    text: str = ''


class KeywordMemoryMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    memory_id: TreeID
    node_id: TreeID
    source_id: str
    kind: str
    text: str
    score: float = Field(gt=0, allow_inf_nan=False)


class QuestionMemorySearch:
    def __init__(self, repository: MySQLQuestionTreeRepository, store: MyVectorStore):
        self.repository = repository
        self.store = store
        self._lock = asyncio.Lock()

    def _nodes(self, documents, tree_id, owner_userid):
        nodes=[]
        for document in documents:
            memory=document.memory
            validate_vector(memory.embedding,self.store.dimensions)
            nodes.append(TextNode(id_=memory.id,text=document.text,embedding=list(memory.embedding),metadata={
                'owner_userid':owner_userid,'tree_id':tree_id,'node_id':memory.node_id,'kind':memory.kind}))
        return nodes

    async def refresh(self, tree_id: str, owner_userid: str):
        """独立刷新当前树；数据库读取失败时不提交空缓存，不与节点落盘事务耦合。"""
        async with self._lock:
            documents=await self.repository.load_searchable_memory_documents(tree_id,owner_userid)
            async with self.repository.memory_index_write_scope(tree_id,owner_userid):
                await self.store.replace_scope(self._nodes(documents,tree_id,owner_userid),
                    owner_userid=owner_userid,tree_id=tree_id)
            return len(documents)

    async def search_keywords(self, tree_id: str, owner_userid: str, text: str, *,
                               top_k: int = 5, kind: str | None = None, node_id: str | None = None) -> list[KeywordMemoryMatch]:
        # 只做范围确认和少量候选回查；查询不重刷整树、不重建向量索引。
        await self.repository.load_searchable_memory_documents(tree_id,owner_userid,[])
        hits=await self.store.keyword_query(owner_userid=owner_userid,tree_id=tree_id,text=text,
            top_k=top_k,kind=kind,node_id=node_id)
        if len(hits)>top_k or len({hit.memory_id for hit in hits})!=len(hits):
            raise ValueError('全文查询返回数量或唯一性不合法')
        if not hits:
            return []
        current={doc.memory.id:doc for doc in await self.repository.load_searchable_memory_documents(
            tree_id,owner_userid,[hit.memory_id for hit in hits])}
        matches=[]
        for hit in hits:
            document=current.get(hit.memory_id)
            if hit.owner_userid!=owner_userid or hit.tree_id!=tree_id:
                raise ValueError('全文索引返回了范围外的记忆')
            if document is None:
                continue
            memory=document.memory
            if hit.node_id!=memory.node_id or hit.kind!=memory.kind:
                raise ValueError('全文索引引用与权威记忆不一致')
            if (kind is not None and hit.kind!=kind) or (node_id is not None and hit.node_id!=node_id):
                raise ValueError('全文索引返回了范围外的记忆')
            if document.text!=hit.text:
                continue
            matches.append(KeywordMemoryMatch(memory_id=memory.id,node_id=memory.node_id,
                source_id=memory.source_id,kind=memory.kind,text=hit.text,score=hit.score))
        return matches

    async def search(self, tree_id: str, owner_userid: str, vector: list[float] | tuple[float, ...], *,
                      top_k: int = 5, min_similarity: float = -1, kind: str | None = None,
                      node_id: str | None = None) -> list[MemoryMatch]:
        validate_vector(vector,self.store.dimensions)
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError('top_k 必须为 1 至 100 的整数')
        if type(min_similarity) not in (float,int) or not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1:
            raise ValueError('相似度阈值不合法')
        scope = {'owner_userid':owner_userid,'tree_id':tree_id}
        if kind is not None:
            scope['kind']=kind
        if node_id is not None:
            scope['node_id']=node_id
        MemoryIndexMetadata.model_validate({**scope,'node_id':node_id if node_id is not None else '0'*32,
            'kind':kind if kind is not None else 'question'})
        query = VectorStoreQuery(query_embedding=list(vector),similarity_top_k=top_k,
            filters=MetadataFilters(filters=[MetadataFilter(key=key,value=value) for key,value in scope.items()]))
        async with self._lock:
            # 数据库失败直接上抛，绝不能误用 [] 删除合法缓存。
            documents = await self.repository.load_searchable_memory_documents(tree_id,owner_userid)
            original = {document.memory.id:document for document in documents}
            nodes = self._nodes(documents,tree_id,owner_userid)
            async with self.repository.memory_index_write_scope(tree_id,owner_userid):
                result = await self.store.replace_and_query(nodes,query)
            if (result.ids is None or result.nodes is None or result.similarities is None
                    or len(result.ids) != len(result.nodes) or len(result.ids) != len(result.similarities)
                    or len(set(result.ids)) != len(result.ids) or len(result.ids)>top_k):
                raise ValueError('向量查询返回不一致的结果')
            if not result.ids:
                return []
            # 缓存没有权限裁决权；删除、状态变化、内容变化和归属变化均需回查过滤。
            current = {doc.memory.id:doc for doc in
                       await self.repository.load_searchable_memory_documents(tree_id,owner_userid,result.ids)}
            matches=[]
            for memory_id,node,similarity in zip(result.ids,result.nodes,result.similarities):
                document=current.get(memory_id)
                if document is None or memory_id not in original or document != original[memory_id]:
                    continue
                memory=document.memory
                expected={'owner_userid':owner_userid,'tree_id':tree_id,'node_id':memory.node_id,'kind':memory.kind}
                if node.node_id != memory_id or node.metadata != expected:
                    raise ValueError('索引引用与权威记忆不一致')
                if node.text!=document.text:
                    raise ValueError('索引原文与权威消息不一致')
                if kind is not None and memory.kind!=kind or node_id is not None and memory.node_id!=node_id:
                    raise ValueError('索引返回了范围外的记忆')
                match=MemoryMatch(memory_id=memory_id,node_id=memory.node_id,source_id=memory.source_id,
                    kind=memory.kind,similarity=similarity,text=node.text)
                if match.similarity>=min_similarity:
                    matches.append(match)
            return matches
