"""记忆结果集复用公共 LRU，仍只保留排名，每次读页回查权威任务。"""
from ...query_pool import QueryResultPool
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
        return super().prepare(rankings)

    async def _render_page(self, rankings, **metadata):
        ids = [i for i,_ in rankings]
        tasks = await self.reader(ids)
        if [task.write.node.id for task in tasks] != ids:
            raise ValueError('历史任务已变化，请重新检索。')
        return render_memory_page(tasks, **metadata)
