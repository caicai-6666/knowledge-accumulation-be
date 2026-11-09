"""从记忆查询池提取的结果集 LRU；各业务独立实例，提交后发布和推进页码。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4


@dataclass
class QueryEntry:
    items: tuple
    current_page: int = 0


class QueryViewRejected(ValueError):
    """查询池就近校验提供的定向反馈；调用方不解析异常字符串。"""

    def __init__(self, reason, required_action):
        super().__init__(reason)
        self.reason = reason
        self.required_action = required_action


class QueryResultPool:
    """只负责快照、分页和生命周期；正文读取/渲染由业务注入，不共享任何池实例。"""
    def __init__(self, render_page, *, capacity=10, page_size=3):
        if type(capacity) is not int or capacity < 1 or type(page_size) is not int or page_size < 1:
            raise ValueError('查询驻留容量和每页条数必须为正整数')
        self.render_page, self.capacity, self.page_size = render_page, capacity, page_size
        self._entries = OrderedDict()
        self._lock = asyncio.Lock()
        self.closed = False

    def prepare(self, items):
        if self.closed or not items:
            raise ValueError('空结果或已关闭任务不能建立查询')
        query_id, entry = str(uuid4()), QueryEntry(tuple(items))
        def commit():
            # 取消/关闭后迟到的回调不能重新建立池。
            if not self.closed:
                self._entries[query_id] = entry
                while len(self._entries) > self.capacity:
                    self._entries.popitem(last=False)
        return query_id, commit

    def close(self):
        self.closed = True
        self._entries.clear()

    async def view(self, query_id, page=None):
        async with self._lock:
            entry = self._entries.get(query_id)
            if self.closed or entry is None:
                raise QueryViewRejected('查询结果不存在或已释放，未读取页面，页码未推进。',
                    '请重新查找并使用返回的完整查询 ID，不复用其他任务或已失效的查询 ID。')
            total = len(entry.items)
            pages = (total + self.page_size - 1) // self.page_size
            target = entry.current_page + 1 if page is None else page
            if type(target) is not int or not 1 <= target <= pages:
                raise QueryViewRejected('请求页码超出结果集范围，未读取页面，页码未推进。',
                    f'请指定 1 至 {pages} 之间的整数页码重看；省略页码且已到末页时，不要继续请求下一页。')
            start = (target - 1) * self.page_size
            text = await self.render_page(entry.items[start:start+self.page_size],
                page=target, total_pages=pages, total=total, start_rank=start+1)
            if self.closed or self._entries.get(query_id) is not entry:
                raise QueryViewRejected('读取期间查询结果已释放，本次页面未接受，页码未推进。',
                    '请重新查找并使用新返回的查询 ID 查看。')
            def commit():
                if not self.closed and self._entries.get(query_id) is entry:
                    entry.current_page = target
                    self._entries.move_to_end(query_id)
            return {'query_id':query_id, 'page':target, 'total_pages':pages,
                    'total':total, 'has_next':target < pages}, text, commit
