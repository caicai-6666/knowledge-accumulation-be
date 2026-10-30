"""探索树共用的结构 LRU；只发布已提交快照，正文与工作区仍按所选路径读取。"""

from collections import OrderedDict


class QuestionTreeCache:
    def __init__(self, capacity=32):
        self._entries = OrderedDict()
        self.epoch = 0
        self.resize(capacity)

    def resize(self, capacity):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('驻留树容量必须为正整数')
        self.capacity = capacity
        while len(self._entries) > capacity:
            self._entries.popitem(last=False)

    def peek(self, owner, tree_id):
        key = (owner, tree_id)
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def get(self, tree):
        snapshot = self.peek(tree.owner_userid, tree.id)
        # 调用方先在当前事务读权威树行；缓存不得替代归属校验或跨版本复用。
        return snapshot if snapshot is not None and snapshot.tree == tree else None

    def publish(self, snapshot, epoch):
        if epoch != self.epoch or snapshot.tree.kind != 'exploration':
            return
        key = (snapshot.tree.owner_userid, snapshot.tree.id)
        old = self._entries.get(key)
        # 较早开始的只读事务可能较晚结束，不能覆盖已经提交的新版本。
        if old is not None and old.tree.revision > snapshot.tree.revision:
            return
        self._entries[key] = snapshot
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def discard(self, owner, tree_id):
        self._entries.pop((owner, tree_id), None)
        # 取消此前事务的迟到发布资格，避免删除/不确定提交后重新驻留旧树。
        self.epoch += 1

    def clear(self):
        self._entries.clear()
        self.epoch += 1
