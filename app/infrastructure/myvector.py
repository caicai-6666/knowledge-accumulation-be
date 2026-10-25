"""带原文副本的检索缓存：MyVector 向量查询及原生 ngram 全文查询。"""

import asyncio
import json
import math
import re
from typing import Literal

import asyncmy
from asyncmy.cursors import DictCursor
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore, FilterCondition, FilterOperator, MetadataFilter,
    VectorStoreQuery, VectorStoreQueryMode, VectorStoreQueryResult,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter

from app.model.question_tree import TreeID
from app.service.embedding import validate_vector

_ID = TypeAdapter(TreeID)


class MemoryIndexMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    owner_userid: str = Field(strict=True, min_length=1, max_length=128)
    tree_id: TreeID
    node_id: TreeID
    kind: Literal['question','intermediate','final']


class KeywordIndexHit(MemoryIndexMetadata):
    memory_id: TreeID
    text: str = Field(strict=True, min_length=1)
    score: float = Field(gt=0, allow_inf_nan=False)


class MyVectorStore(BasePydanticVectorStore):
    # 原文是可重建副本；权限、来源及内容真实性仍由业务仓库回查。
    stores_text: bool = True
    dimensions: int = Field(ge=1, le=4096)
    search_mode: Literal['hnsw','exact'] = 'hnsw'
    candidate_limit: int = Field(default=10000, ge=100, le=10000)
    operation_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    _pool: asyncmy.Pool = PrivateAttr()
    _operation_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _index_dirty: bool = PrivateAttr(default=True)

    def __init__(self, pool, **kwargs):
        super().__init__(**kwargs)
        self._pool = pool

    @property
    def client(self):
        return self._pool

    def add(self, nodes, **kwargs):
        raise NotImplementedError('请使用异步向量方法')

    def query(self, query, **kwargs):
        raise NotImplementedError('请使用异步向量方法')

    def delete(self, ref_doc_id, **kwargs):
        raise NotImplementedError('记忆缓存按用户及树刷新，不接受无归属删除')

    def _values(self, nodes):
        values = []
        for node in nodes:
            metadata = MemoryIndexMetadata.model_validate(node.metadata)
            memory_id = _ID.validate_python(node.node_id)
            vector = node.get_embedding()
            validate_vector(vector, self.dimensions)
            values.append((memory_id, metadata.owner_userid, metadata.tree_id,
                           metadata.node_id, metadata.kind, json.dumps(vector, allow_nan=False),node.text))
        if len({value[0] for value in values}) != len(values):
            raise ValueError('索引快照包含重复记忆')
        return values

    @staticmethod
    def _autocommit(connection):
        if not connection.get_autocommit():
            raise ValueError('MyVector 必须使用独立 autocommit=True 连接池')

    async def _write(self, values, *, scope=None):
        self._index_dirty = True
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            self._autocommit(connection)
            await connection.begin()
            try:
                if scope is not None:
                    await cursor.execute('DELETE FROM question_memory_index WHERE owner_userid=%s AND tree_id=%s', scope)
                if values:
                    await cursor.executemany('INSERT INTO question_memory_index '
                        '(memory_id,owner_userid,tree_id,node_id,kind,embedding,text) '
                        'VALUES(%s,%s,%s,%s,%s,myvector_construct(%s),%s) AS incoming '
                        'ON DUPLICATE KEY UPDATE owner_userid=incoming.owner_userid,tree_id=incoming.tree_id,'
                        'node_id=incoming.node_id,kind=incoming.kind,embedding=incoming.embedding,text=incoming.text', values)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def async_add(self, nodes, **kwargs):
        values = self._values(nodes)
        if not values:
            return []
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            await self._write(values)
        return [value[0] for value in values]

    async def replace_scope(self, nodes, *, owner_userid: str, tree_id: str):
        """供节点提交后的独立加工阶段显式刷新；节点事务不等待全文索引。"""
        MemoryIndexMetadata(owner_userid=owner_userid,tree_id=tree_id,node_id='0'*32,kind='question')
        values=self._values(nodes)
        if any(value[1:3] != (owner_userid,tree_id) for value in values):
            raise ValueError('索引快照包含其他用户或树')
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            await self._write(values,scope=(owner_userid,tree_id))

    async def keyword_query(self, *, owner_userid: str, tree_id: str, text: str,
                            top_k: int = 5, kind: str | None = None, node_id: str | None = None):
        """自然语言模式接收整句；不把用户输入解释为布尔运算符。"""
        if not isinstance(text,str) or not text.strip() or len(text)>4096:
            raise ValueError('查询文本必须为 1 至 4096 字符的非空文本')
        if type(top_k) is not int or not 1<=top_k<=100:
            raise ValueError('top_k 必须为 1 至 100 的整数')
        scope={'owner_userid':owner_userid,'tree_id':tree_id}
        if kind is not None:
            scope['kind']=kind
        if node_id is not None:
            scope['node_id']=node_id
        MemoryIndexMetadata.model_validate({'node_id':'0'*32,'kind':'question',**scope})
        conditions=' AND '.join(f'{key}=%s' for key in sorted(scope))
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                self._autocommit(connection)
                await cursor.execute('SELECT '+self._columns()+',MATCH(text) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score '
                    'FROM question_memory_index WHERE '+conditions+
                    ' AND MATCH(text) AGAINST(%s IN NATURAL LANGUAGE MODE)>0 ORDER BY score DESC,memory_id LIMIT %s',
                    [text,*[scope[key] for key in sorted(scope)],text,top_k])
                return [KeywordIndexHit.model_validate(row) for row in await cursor.fetchall()]

    async def rank_memory_snapshot(self, nodes, *, owner_userid, tree_id, node_ids, queries, top_k=20):
        """整树缓存刷新与各路祖先范围排名共用锁；先按任务聚合，再截断排名。"""
        MemoryIndexMetadata(owner_userid=owner_userid, tree_id=tree_id, node_id='0'*32, kind='question')
        if type(top_k) is not int or not 1 <= top_k <= 20 or not 1 <= len(queries) <= 3:
            raise ValueError('记忆查询数量不合法')
        identifiers = sorted({_ID.validate_python(i) for i in node_ids})
        if not identifiers:
            return []
        for kind, text, vector in queries:
            MemoryIndexMetadata(owner_userid=owner_userid, tree_id=tree_id, node_id=identifiers[0], kind=kind)
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise ValueError('查询正文不合法')
            validate_vector(vector, self.dimensions)
        values = self._values(nodes)
        if any(value[1:3] != (owner_userid, tree_id) for value in values):
            raise ValueError('记忆缓存快照归属不一致')
        where = 'owner_userid=%s AND tree_id=%s AND kind=%s AND node_id IN ('+','.join(['%s']*len(identifiers))+')'
        routes = []
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            await self._write(values, scope=(owner_userid, tree_id))
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                self._autocommit(connection)
                for kind, text, vector in queries:
                    # 同任务多条中途消息只贡献一个排名，避免长任务占满候选预算。
                    await cursor.execute('SELECT node_id,MAX(MATCH(text) AGAINST(%s IN NATURAL LANGUAGE MODE)) AS score '
                        'FROM question_memory_index WHERE '+where+' GROUP BY node_id HAVING score>0 '
                        'ORDER BY score DESC,node_id LIMIT %s', [text, owner_userid, tree_id, kind, *identifiers, top_k])
                    routes.append([(row['node_id'], float(row['score'])) for row in await cursor.fetchall()])
                    # 与参考实现一样在已限定范围内精确余弦排序，避免全局 ANN 干扰祖先召回。
                    await cursor.execute("SELECT node_id,MIN(myvector_distance(embedding,myvector_construct(%s),'Cosine')) AS distance "
                        'FROM question_memory_index WHERE '+where+' GROUP BY node_id ORDER BY distance,node_id LIMIT %s',
                        [json.dumps(vector, allow_nan=False), owner_userid, tree_id, kind, *identifiers, top_k])
                    routes.append([(row['node_id'], 1-float(row['distance'])) for row in await cursor.fetchall()])
        if any(not math.isfinite(score) for route in routes for _, score in route):
            raise ValueError('检索返回非有限分数')
        return routes

    def _conditions(self, query):
        if (query.mode != VectorStoreQueryMode.DEFAULT or query.query_embedding is None
                or query.node_ids or query.doc_ids or query.sparse_top_k is not None):
            raise ValueError('当前仅支持带归属过滤的普通向量查询')
        validate_vector(query.query_embedding, self.dimensions)
        if type(query.similarity_top_k) is not int or not 1 <= query.similarity_top_k <= 100:
            raise ValueError('top_k 必须为 1 至 100 的整数')
        filters = query.filters
        if filters is None or filters.condition != FilterCondition.AND:
            raise ValueError('查询必须按用户和树进行 AND 过滤')
        supplied = {}
        allowed = {'owner_userid','tree_id','node_id','kind'}
        for item in filters.filters:
            if (not isinstance(item, MetadataFilter) or item.key not in allowed
                    or item.operator != FilterOperator.EQ or item.key in supplied or not isinstance(item.value,str)):
                raise ValueError('只允许用户、树、节点和类型的单次相等过滤')
            supplied[item.key] = item.value
        if not {'owner_userid','tree_id'} <= supplied.keys():
            raise ValueError('用户与树过滤不可省略')
        MemoryIndexMetadata.model_validate({
            **supplied, 'node_id':supplied.get('node_id','0'*32), 'kind':supplied.get('kind','question')})
        conditions = [f'{key}=%s' for key in sorted(supplied)]
        return conditions, [supplied[key] for key in sorted(supplied)], supplied

    async def replace_and_query(self, nodes, query):
        """快照刷新→重建→查询共用同一实例锁，避免本实例内切换树时串用快照。"""
        _, _, scope = self._conditions(query)
        values = self._values(nodes)
        if any(value[1:3] != (scope['owner_userid'],scope['tree_id']) for value in values):
            raise ValueError('索引快照包含其他用户或树')
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            await self._write(values, scope=(scope['owner_userid'],scope['tree_id']))
            if not values:
                return VectorStoreQueryResult(nodes=[],ids=[],similarities=[])
            return await self._query(query)

    async def aquery(self, query, **kwargs):
        self._conditions(query)
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            return await self._query(query)

    async def _query(self, query):
        conditions, params, _ = self._conditions(query)
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            self._autocommit(connection)
            if self.search_mode == 'hnsw' and self._index_dirty:
                await cursor.execute('SELECT COUNT(*) AS n FROM question_memory_index')
                if not (await cursor.fetchone())['n']:
                    return VectorStoreQueryResult(nodes=[],ids=[],similarities=[])
                await self._rebuild(cursor)
            vector = json.dumps(query.query_embedding, allow_nan=False)
            if self.search_mode == 'exact':
                rows = await self._exact(cursor,conditions,params,vector,query.similarity_top_k)
            else:
                rows = await self._hnsw(cursor,conditions,params,vector,query.similarity_top_k)
        similarities = []
        nodes = []
        for row in rows:
            distance = float(row['distance'])
            if not math.isfinite(distance):
                raise ValueError('插件返回非有限距离')
            metadata = MemoryIndexMetadata.model_validate({key:row[key] for key in ('owner_userid','tree_id','node_id','kind')})
            nodes.append(TextNode(id_=_ID.validate_python(row['memory_id']),text=row['text'],metadata=metadata.model_dump()))
            similarities.append(max(-1.0,min(1.0,1.0-distance)))
        return VectorStoreQueryResult(nodes=nodes,ids=[node.node_id for node in nodes],similarities=similarities)

    async def _rebuild(self, cursor):
        await cursor.execute("SELECT COLUMN_COMMENT AS options FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='question_memory_index' AND COLUMN_NAME='embedding'")
        definition = await cursor.fetchone()
        options = definition['options'] if definition else ''
        dimension = re.search(r'\bdim=(\d+)',options)
        capacity = re.search(r'\bsize=(\d+)',options)
        if not dimension or int(dimension[1]) != self.dimensions or not capacity:
            raise ValueError('MyVector 索引维度或容量声明不合法')
        await cursor.execute('SELECT COUNT(*) AS n FROM question_memory_index')
        if (await cursor.fetchone())['n'] > int(capacity[1]):
            raise ValueError('MyVector 索引容量不足，需显式扩容')
        await cursor.execute('SELECT DATABASE() AS name')
        index = (await cursor.fetchone())['name']+'.question_memory_index.embedding'
        await cursor.execute("CALL mysql.myvector_index_build(%s,'id')",(index,))
        result = await cursor.fetchall()
        while await cursor.nextset():
            pass
        if not any('SUCCESS' in str(row) for row in result):
            raise RuntimeError('MyVector 索引重建失败')
        await cursor.execute('CALL mysql.myvector_index_status(%s)',(index,))
        status='\n'.join(str(value) for row in await cursor.fetchall() for value in row.values())
        while await cursor.nextset():
            pass
        if 'Type : HNSW' not in status or f'Dimension : {self.dimensions}\n' not in status or 'Distance : Cosine' not in status:
            raise ValueError('MyVector 索引类型、维度或距离配置不匹配')
        self._index_dirty = False

    async def rebuild_index(self):
        async with asyncio.timeout(self.operation_timeout_seconds), self._operation_lock:
            self._index_dirty = True
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                self._autocommit(connection)
                await self._rebuild(cursor)

    @staticmethod
    def _columns():
        return 'memory_id,owner_userid,tree_id,node_id,kind,text'

    async def _exact(self,cursor,conditions,params,vector,top_k):
        await cursor.execute('SELECT '+self._columns()+
            ",myvector_distance(embedding,myvector_construct(%s),'Cosine') AS distance "
            'FROM question_memory_index WHERE '+' AND '.join(conditions)+' ORDER BY distance,memory_id LIMIT %s',
            [vector,*params,top_k])
        return await cursor.fetchall()

    async def _hnsw(self,cursor,conditions,params,vector,top_k):
        await cursor.execute('SELECT DATABASE() AS name')
        index=(await cursor.fetchone())['name']+'.question_memory_index.embedding'
        await cursor.execute('SELECT COUNT(*) AS n FROM question_memory_index')
        total=(await cursor.fetchone())['n']
        if not total:
            return []
        count=min(total,max(100,top_k*10),self.candidate_limit)
        while True:
            # 距离缓存属于数据库连接，ANN 和 row_distance 必须在同一 SQL 中读取。
            await cursor.execute('SELECT '+self._columns()+',myvector_row_distance(id) AS distance '
                'FROM question_memory_index WHERE '+' AND '.join(conditions)+
                ' AND id IN (SELECT candidate_id FROM '
                '(SELECT myvector_ann_set(%s,\'id\',myvector_construct(%s),%s) AS candidates) AS result, '
                "JSON_TABLE(result.candidates,'$[*]' COLUMNS(candidate_id BIGINT UNSIGNED PATH '$')) AS ids) "
                'ORDER BY distance,memory_id LIMIT %s',[*params,index,vector,f'nn={count}',top_k])
            rows=await cursor.fetchall()
            if len(rows)>=top_k or count>=total:
                return rows
            if count>=self.candidate_limit:
                return await self._exact(cursor,conditions,params,vector,top_k)
            count=min(total,count*2,self.candidate_limit)
