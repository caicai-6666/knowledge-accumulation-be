"""纯内存文件检索：当前授权快照的BM25与余弦排名，摘要向量有限LRU复用。"""
import asyncio
from collections import Counter, OrderedDict
import math
import re
import unicodedata


def tokenize(text):
    """统一大小写/全半角；汉字保留单字和相邻双字，其他文字按连续字母数字切分。"""
    tokens=[]
    for part in re.findall(r'[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+',unicodedata.normalize('NFKC',text).casefold()):
        if all('\u3400' <= c <= '\u9fff' for c in part):
            tokens.extend(part)
            tokens.extend(part[i:i+2] for i in range(len(part)-1))
        else:
            tokens.append(part)
    return Counter(tokens)


def normalize_vector(values):
    if not values or any(type(v) not in (float,int) or not math.isfinite(v) for v in values):
        raise ValueError('文件检索向量不合法')
    norm=math.hypot(*values)
    if not math.isfinite(norm) or norm==0:
        raise ValueError('文件检索向量不合法')
    return tuple(v/norm for v in values)


def rank_snapshot(documents, query, field, vector, limit):
    terms=tokenize(query)
    counters={key:entry[field] for key,entry in documents.items()}
    lengths={key:sum(c.values()) for key,c in counters.items()}
    count=len(counters)
    average=sum(lengths.values())/count if count else 0
    frequencies=Counter(t for c in counters.values() for t in c)
    lexical=[]
    if average:
        # BM25(k1=1.2,b=.75)，正值平滑IDF。统计仅来自本次授权范围，其他用户缓存不参与。
        for key,counts in counters.items():
            score=0.
            for term in terms:
                tf=counts[term]
                if tf:
                    df=frequencies[term]
                    score+=math.log1p((count-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*lengths[key]/average))
            if score>0:lexical.append((key,score))
    vectors=[]
    if vector is not None:
        query_vector=normalize_vector(vector)
        for key,entry in documents.items():
            stored=entry['vector']
            if stored is None or len(stored)!=len(query_vector):
                raise ValueError('文件摘要向量缺失或维度不符')
            score=math.fsum(a*b for a,b in zip(stored,query_vector))
            vectors.append((key,max(-1.,min(1.,score))))
    return tuple(sorted(lane,key=lambda item:(-item[1],item[0]))[:limit] for lane in (lexical,vectors))


class MemoryFileSearchIndex:
    """调用方在同一维护锁内执行ensure→编码→rank→clear_documents；不接收数据库依赖。"""
    def __init__(self, *, max_vectors=256):
        if type(max_vectors) is not int or max_vectors<1:
            raise ValueError('摘要向量驻留容量须为正整数')
        self.max_vectors=max_vectors
        self._vectors=OrderedDict()
        self._documents={}
        self.closed=False

    def _require_open(self):
        if self.closed:raise RuntimeError('文件检索内存池已关闭')

    async def ensure(self, documents):
        self._require_open()
        def prepare():
            return {key:{'name':tokenize(name),'abstract':tokenize(abstract),'vector':None}
                for key,name,abstract in documents}
        prepared=await asyncio.to_thread(prepare)
        self._require_open()
        for key,entry in prepared.items():
            if key in self._vectors:
                entry['vector']=self._vectors[key]
                self._vectors.move_to_end(key)
        self._documents=prepared

    async def missing_vectors(self, keys):
        self._require_open()
        return {key for key in keys if self._documents[key]['vector'] is None}

    async def save_vectors(self, values):
        self._require_open()
        normalized=[(key,normalize_vector(vector)) for key,vector in values]
        if any(key not in self._documents for key,_ in normalized):
            raise ValueError('向量不属于当前授权快照')
        for key,vector in normalized:
            self._documents[key]['vector']=vector
            self._vectors[key]=vector
            self._vectors.move_to_end(key)
            while len(self._vectors)>self.max_vectors:
                self._vectors.popitem(last=False)
        # 单次授权范围可能超过LRU容量；当前快照仍持有全部在用向量，不能因此丢掉候选。

    async def rank(self, keys, query, field, vector, limit):
        self._require_open()
        if field not in ('name','abstract') or type(limit) is not int or limit<1:
            raise ValueError('文件检索排名参数不合法')
        snapshot={key:self._documents[key] for key in keys}
        result=await asyncio.to_thread(rank_snapshot,snapshot,query,field,vector,limit)
        self._require_open()
        return result

    def clear_documents(self):
        self._documents.clear()

    def close(self):
        self.closed=True
        self.clear_documents()
        self._vectors.clear()
