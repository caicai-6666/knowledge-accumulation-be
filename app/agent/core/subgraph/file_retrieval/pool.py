"""候选引用及最终分页池；只驻留排名，不缓存文件名、摘要或权限。"""
from collections import OrderedDict
from dataclasses import dataclass
import asyncio
import json
import re
from uuid import uuid4
from .ranking import rank_candidates, union_candidates


class CandidatePool:
    def __init__(self, *, initial=None, initial_ranking=None, capacity=10, k=60, history_weight=0.5):
        self.initial = None if initial is None else frozenset(initial)
        self.initial_ranking = initial_ranking
        self.capacity, self.k, self.history_weight = capacity,k,history_weight
        self.entries = OrderedDict()
        self.closed = False

    def get(self, rid):
        if self.closed or rid not in self.entries:
            raise ValueError('结果集不存在或已失效，请重新查询；不能回退全库。')
        self.entries.move_to_end(rid)
        return self.entries[rid]

    def scope(self, rid):
        if self.closed:
            raise ValueError('查询已结束')
        return tuple(i for i,_ in self.get(rid).hits) if rid is not None else self.initial

    def _store(self, ranking):
        if self.closed or (self.initial is not None and not {i for i,_ in ranking.hits} <= self.initial):
            raise ValueError('候选超出初始范围或查询已结束')
        if any(not re.fullmatch(r'[1-9][0-9]*',i) for i,_ in ranking.hits):
            raise ValueError('文件ID不是注册中心正式资源ID')
        rid = 'candidate-set:'+str(uuid4())
        self.entries[rid] = ranking
        while len(self.entries)>self.capacity:
            self.entries.popitem(last=False)
        return rid

    def put(self, hits, kind, parent=None, *, exact=False):
        old = self.get(parent) if parent is not None else self.initial_ranking
        return self._store(rank_candidates(hits,search_type=kind,parent=old,exact=exact,k=self.k,history_weight=self.history_weight))

    def union(self, left, right):
        a,b = self.get(left),self.get(right)
        return left if left==right else self._store(union_candidates(a,b,k=self.k))

    def close(self):
        self.closed=True
        self.entries.clear()


@dataclass
class FinalEntry:
    ranking: object
    platforms: tuple | None
    explanation: str
    page: int = 0


def plain_name(value):
    # 名称是数据，不能用换行、HTML或Markdown伪造列表项和模型指令标题。
    value = ' '.join((value or '未命名文件').split())
    return re.sub(r'([\\`*_{}\[\]()#+!|>~<])',r'\\\1',value)


def passport_list(values):
    """通行证及来源平台标识展示为代码文本，保留原值，不让业务值变成 Markdown。"""
    if values is None:
        return '资料未提供'
    if not values:
        return '暂无'
    rendered=[]
    for value in values:
        # 控制字符用可见转义；动态反引号边界避免值中的反引号提前结束代码片段。
        display=json.dumps(value,ensure_ascii=False)[1:-1]
        fence='`'*(max((len(run) for run in re.findall(r'`+',display)),default=0)+1)
        rendered.append(f'{fence} {display} {fence}')
    return '、'.join(rendered)


class FileResultError(ValueError):
    """可向模型展示的本地结果引用/页码错误。"""


class FileResultPool:
    def __init__(self, reader, *, capacity=10, page_size=5):
        if min(capacity,page_size)<1:
            raise FileResultError('分页与容量必须为正整数')
        self.reader,self.capacity,self.page_size = reader,capacity,page_size
        self.entries = OrderedDict()
        self.closed = False
        self.lock = asyncio.Lock()

    def get(self, rid):
        if self.closed or rid not in self.entries:
            raise FileResultError('结果集不存在或已释放，请重新检索。')
        self.entries.move_to_end(rid)
        return self.entries[rid]

    def check(self, rid, entry):
        if self.get(rid) is not entry:
            raise FileResultError('结果集已变化')

    def prepare(self, ranking, platforms, explanation, *, parent_id=None, parent=None):
        if self.closed or not ranking.hits:
            raise FileResultError('空结果或已关闭任务不能发布')
        if parent_id is not None:
            self.check(parent_id,parent)
        rid = 'file-query:'+str(uuid4())
        entry = FinalEntry(ranking,platforms,explanation)
        def commit():
            if self.closed:
                return
            if parent_id is not None:
                self.check(parent_id,parent)
            self.entries[rid]=entry
            while len(self.entries)>self.capacity:
                self.entries.popitem(last=False)
        return rid,commit

    async def view(self, rid, page=None):
        async with self.lock:
            entry=self.get(rid)
            total=len(entry.ranking.hits)
            pages=(total+self.page_size-1)//self.page_size
            target=entry.page+1 if page is None else page
            if page is None and target>pages:
                raise FileResultError('已经到达末页；可以指定页码重看。')
            if type(target) is not int or not 1<=target<=pages:
                raise FileResultError('页码超出结果集范围。')
            start=(target-1)*self.page_size
            ids=[i for i,_ in entry.ranking.hits[start:start+self.page_size]]
            rows={row.id:row for row in await self.reader(ids,entry.platforms)}
            if not set(rows)<=set(ids):
                raise FileResultError('回读结果越界')
            self.check(rid,entry)
            lines=['# 文件查询结果','',f'第 {target} / {pages} 页 · 本次召回 {total} 个候选','']
            for offset,i in enumerate(ids,start+1):
                row=rows.get(i)
                if row is None:
                    # 删除或撤权时连ID也不重放；保留原排名位置，不据此猜测是哪一种变化。
                    lines.extend([f'{offset}. 此候选已不可用，请重新检索。',''])
                else:
                    lines.extend([f'{offset}. **文件名称**：{plain_name(row.name)}  ',f'   **文件 ID**：`{i}`  ',
                        f'   **来源平台代码**：{plain_name(row.platform)}  ',
                        f'   **来源平台文件 ID**：{passport_list(None if row.source_id is None else [row.source_id])}  ',
                        f'   **通行证**：{passport_list(row.passport)}',''])
            lines.append('还有下一页。' if target<pages else '已到最后一页。')
            def commit():
                if not self.closed and self.entries.get(rid) is entry:
                    entry.page=target
            return {'result_id':rid,'page':target,'total_pages':pages,'count':total,'has_next':target<pages},'\n'.join(lines),commit

    def close(self):
        self.closed=True
        self.entries.clear()
