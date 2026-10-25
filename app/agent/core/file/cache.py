"""文件模型 LRU 池；参考实现的租约、单模型所有权与关闭等待边界。"""
import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition,RLock
from .pdf import FileModel

_OWNERSHIP_LOCK=RLock()


@dataclass(frozen=True)
class FileKey:
    scope:tuple[str,str]
    file_id:str
    # 本地上传 UUID 对应不可变文件，不需要内容摘要；远程来源仍可显式携带版本。
    digest:str|None=None


@dataclass
class _Entry:
    model:FileModel
    readers:int=0


class FileCacheBusyError(RuntimeError):pass


class FileModelCache:
    def __init__(self,*,max_files=4):
        if type(max_files) is not int or max_files<1:raise ValueError('文件驻留上限必须为正整数')
        self.max_files=max_files
        self._entries=OrderedDict()
        self._condition=Condition(_OWNERSHIP_LOCK)
        self._closing=False
        self._closed=False

    def _require_open(self):
        if self._closing or self._closed:raise RuntimeError('文件池已关闭或正在关闭')

    @property
    def closed(self):
        with self._condition:return self._closed

    def keys(self):
        """只供宿主管理与测试的 LRU 顺序，不进入模型上下文。"""
        with self._condition:return tuple(self._entries)

    def _remove(self,key):
        entry=self._entries[key]
        entry.model.close()
        del self._entries[key]
        entry.model._cache_owner=None

    def put(self,key,model):
        if not isinstance(key,FileKey) or not isinstance(model,FileModel):raise TypeError('文件池输入不合法')
        with self._condition:
            self._require_open()
            if model.closed:raise ValueError('不能加入已关闭模型')
            old=self._entries.get(key)
            if old is not None and old.model is model:return
            if model._cache_owner is not None:raise ValueError('模型已归属文件池')
            if old is not None:
                if old.readers:raise FileCacheBusyError('文件正在使用，不能替换')
                self._remove(key)
            elif len(self._entries)>=self.max_files:
                victim=next((item for item,entry in self._entries.items() if not entry.readers),None)
                if victim is None:raise FileCacheBusyError('文件池已满，全部文件正在使用')
                self._remove(victim)
            self._entries[key]=_Entry(model)
            model._cache_owner=self

    @contextmanager
    def acquire(self,key):
        with self._condition:
            self._require_open()
            entry=self._entries[key]
            entry.readers+=1
            self._entries.move_to_end(key)
        try:yield entry.model
        finally:
            with self._condition:
                entry.readers-=1
                self._condition.notify_all()

    @contextmanager
    def acquire_or_load(self,key,load):
        # 首次加载与建立租约同锁完成，合并同键并发，避免刚入池便被其他查看器驱逐。
        with self._condition:
            self._require_open()
            if key not in self._entries:
                model=load()
                try:self.put(key,model)
                except BaseException:
                    model.close()
                    raise
            lease=self.acquire(key)
            model=lease.__enter__()
        try:yield model
        finally:lease.__exit__(None,None,None)

    def close(self):
        with self._condition:
            if self._closed:return
            if self._closing:
                self._condition.wait_for(lambda:self._closed or not self._closing)
                return self.close()
            self._closing=True
            self._condition.wait_for(lambda:all(not entry.readers for entry in self._entries.values()))
            errors=[]
            for key in tuple(self._entries):
                try:self._remove(key)
                except Exception as error:errors.append(error)
            self._closed=not errors
            if errors:self._closing=False
            self._condition.notify_all()
            if errors:raise ExceptionGroup('文件资源释放失败',errors)

    async def aclose(self):
        # 取消等待不强制关闭正在渲染的 MuPDF；工作线程归还租约后完成释放。
        await asyncio.to_thread(self.close)
