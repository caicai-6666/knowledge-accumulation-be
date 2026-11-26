# 助理空闲心跳（1B agent-core 原生方案）

本文档说明个人助理基于 `agent-core` 原生 `AgentScheduler` 与原生空转裁剪（`_prune()`）的空闲心跳机制。

---

## 1. 设计决策（1B）

个人助理已移除旧版 `AssistantHeartbeatScheduler` 及 `assistant_pruning.py`，改由 `agent-core` 原生调度器托管空闲心跳：

- **配置映射**：`app/main.py` 将 `AssistantRuntimeSettings.heartbeat_interval_seconds`（`-1` 关闭，正整数为全局心跳间隔秒数）与 `heartbeat_poll_seconds` 封装为 `agent_core.SchedulerSettings`，在 `AgentRuntime.create/restore` 时绑定到每位助理的 `TreeAgent`。
- **全局巡检**：`AssistantSchedulerRunner`（`app/service/assistant_runtime.py`）周期遍历 `assistants` 中 `enabled=1` 的助理，排除已离职员工后在 `user_scope(userid)` 锁内调用 `AgentScheduler.tick()`。

---

## 2. 原生空转裁剪（`prune_current_task`）

- 个人助理定制的 `AssistantFinishTask`（`app/agent/assistant/tool/finish_task.py`）完整满足 `agent_core.core.tool.heartbeat.supports_heartbeat_pruning` 契约（参数含 `send_to_user`、`prune_current_task`、`file_ids`，`output_kind='final'` 且 `deliver_output is not None`）。
- 当心跳任务检查已有记忆与工作区后确认无须行动、未调用修改类工具且未产生值得保留的新结论时，模型可调用 `finish_task(content='...', send_to_user=False, prune_current_task=True)`。
- `agent-core` 校验通过后执行原生 `_prune()`，清除本次空转心跳节点，不污染历史记忆与工作区，也不向用户发送多余消息；若确有需要告知用户的事项，则置 `send_to_user=True, prune_current_task=False`，经钉钉正文转换与 `assistant_deliveries` 可靠送达。

---

## 3. 验证状态

- 已在 `tests/assistant_v2/test_assistant_e2e.py` 中通过 SQLite + Mock DingTalk 验证 `SchedulerSettings(heartbeat_interval_seconds=1)` 下空闲心跳触发及 `prune_current_task=True` 原生 `_prune()` 裁剪（`TreeView.nodes` 数量不变、零多余钉钉消息），并在 `tests/assistant_v2/test_assistant_tools.py` 中断言 `supports_heartbeat_pruning(AssistantFinishTask) == True`。
- 尚未在真实钉钉环境执行线上心跳投递验收。
