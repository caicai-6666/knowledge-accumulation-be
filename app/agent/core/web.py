"""用户树的网页搜索/正文资源；活动任务固定驻留，避免跨用户共享来源与正文。"""
from collections import OrderedDict
from contextlib import contextmanager
from functools import partial

from .subgraph.web_page import build_web_page_subgraph
from .tool.web_search import WebSearchPool, WebSearchService, build_web_search_registrations
from .tool.web_page import WebPageViewer, build_open_web_page_registration


class WebResources:
    def __init__(self, *, settings, options, graph_factory=None):
        self.active = 0
        self.closed = False
        self.search = WebSearchPool(capacity=options.web_search_cache_max_queries,
            page_size=options.web_search_page_size)
        self.pages = WebPageViewer(sources=self.search, settings=settings, authorize=self.authorize,
            capacity=options.web_page_cache_max_entries, page_chars=options.web_page_chars,
            graph_factory=graph_factory)

    async def authorize(self):
        if self.closed:
            raise PermissionError('网页资源已释放')

    def tools(self, service, *, on_audit=None):
        return [*build_web_search_registrations(pool=self.search, service=service, authorize=self.authorize),
            build_open_web_page_registration(self.pages, on_audit=on_audit)]

    def close(self):
        self.closed = True
        self.search.close()
        self.pages.close()


class WebSessions:
    def __init__(self, *, settings, options, search_service=None, graph_factory=None, client=None, max_scopes=32):
        if type(max_scopes) is not int or max_scopes < 1:
            raise ValueError('网页宿主驻留容量必须为正整数')
        self.settings, self.options = settings, options
        self.service = search_service if search_service is not None else WebSearchService(
            concurrency=options.web_search_max_concurrent_requests)
        self.graph_factory = graph_factory or partial(build_web_page_subgraph, client=client,
            image_max_candidates=options.web_image_max_candidates, image_max_selected=options.web_image_max_selected,
            image_max_bytes=options.web_image_max_bytes)
        self.max_scopes = max_scopes
        self._pools = OrderedDict()
        self.closed = False

    @contextmanager
    def lease(self, owner, tree_id):
        if self.closed:
            raise RuntimeError('网页服务已关闭')
        key = (owner, tree_id)
        resources = self._pools.get(key)
        if resources is None:
            if len(self._pools) >= self.max_scopes:
                victim = next((scope for scope, pool in self._pools.items() if not pool.active), None)
                if victim is None:
                    raise RuntimeError('网页宿主均在使用中')
                self.release(*victim)
            resources = WebResources(settings=self.settings, options=self.options, graph_factory=self.graph_factory)
            self._pools[key] = resources
        self._pools.move_to_end(key)
        resources.active += 1
        try:
            with resources.pages.task_scope():
                yield resources
        finally:
            resources.active -= 1

    def release(self, owner, tree_id):
        resources = self._pools.pop((owner, tree_id), None)
        if resources is not None:
            resources.close()

    def close(self):
        self.closed = True
        for resources in self._pools.values():
            resources.close()
        self._pools.clear()
