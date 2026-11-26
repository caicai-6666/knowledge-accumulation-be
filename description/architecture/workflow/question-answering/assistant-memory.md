# 个人助理记忆与工作区管理

本文档说明个人助理在 `agent-core` 运行时下的工作区保留策略、祖先记忆检索与上下文窗口查看机制。

---

## 1. agent-core 原生上下文与工作区

`AssistantRuntimeService`（`app/service/assistant_runtime.py`）为每位员工的唯一 `tree_id` 创建或恢复 `TreeAgent`：

- `execution_mode='queue'`：单线程串行消费队列，支持优先级与 `external_id` 幂等去重。
- `workspace_retention='latest'`：每棵个人助理树仅保留最新一份工作区快照，避免长生命周期单树产生历史版本膨胀。
- `persist_new_nodes=True`：完整任务节点、压缩摘要与向量索引由 `agent-core` 统一写入 `records` / `spaces` / `memory` 表，彻底替代原有的 `assistant_workspaces`、`assistant_memory` 与 `MyVector` 缓存。

---

## 2. 记忆召回与窗口切片工具

1. **混合记忆检索（内置）**：直接启用 `agent-core` 内置工具 `search_memory` 与 `view_memory_query`（通过 `ASSISTANT_BUILTIN_TOOLS` 装配），在当前助理树的 `memory` 表中按向量 + 关键词混合召回祖先任务记忆并分页查看。
2. **连续窗口查看（本地工具）**：`app/agent/assistant/tool/memory_window.py` 提供 `search_memory_window`，在当前助理任务链上按偏移/范围切片读取历史消息并写入 `agent-core` 的 `DEFAULT_MEMORY_CACHE_POOL`（`write_pools=(cache_pool,)`），供内置 `view_memory_query` 分页读取；末尾挂接 `FeedbackOnlyHook` 渲染中文 Markdown。
3. **空转心跳裁剪**：当 `agent-core` 原生心跳任务调用 `finish_task(prune_current_task=True)` 时，由 `agent-core` 原生 `_prune()` 机制清理当前空转节点，不污染历史记忆与工作区。
