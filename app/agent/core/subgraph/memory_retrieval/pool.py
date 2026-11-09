"""记忆结果集复用公共 LRU，只保留有序任务引用，每次读页回查权威任务。"""
import re
from ...query_pool import QueryResultPool, QueryViewRejected
from .rendering import render_memory_page


class MemoryQueryPool(QueryResultPool):
    def __init__(self, reader, *, capacity=10, page_size=3):
        self.reader = reader
        super().__init__(self._render_page, capacity=capacity, page_size=page_size)

    def prepare(self, result):
        if self.closed or result.status != 'success' or not result.tasks:
            raise ValueError('失败或空结果不能建立查询')
        rankings = tuple((task.node_id, task.score) for task in result.tasks)
        if len(rankings)>20 or len({i for i,_ in rankings}) != len(rankings) or any(a[1]<b[1] for a,b in zip(rankings,rankings[1:])):
            raise ValueError('查询排名不合法')
        return self.prepare_tasks(tuple(identifier for identifier, _ in rankings))

    def prepare_tasks(self, ids):
        """窗口保持父链顺序，语义检索保持排名顺序；共用同一分页和提交边界。"""
        ids = tuple(ids)
        if (not 1 <= len(ids) <= 20 or any(not isinstance(i, str) or not re.fullmatch(r'[0-9a-f]{32}', i) for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('记忆任务引用数量、格式或唯一性不合法')
        return super().prepare(ids)

    async def _render_page(self, items, **metadata):
        ids = list(items)
        tasks = await self.reader(ids)
        if [task.write.node.id for task in tasks] != ids:
            raise QueryViewRejected('历史任务回读结果与查询引用不一致，本次页面未接受，页码未推进。',
                '请重新查找历史任务并使用新返回的查询 ID 查看，不依据旧引用推断任务正文。')
        return render_memory_page(tasks, **metadata)
