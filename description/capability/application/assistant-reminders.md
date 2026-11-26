# 助理定时提醒（1B agent-core 内置方案）

本文档说明个人助理采用 `agent-core` 原生提醒能力（1B 方案）的创建、查询、删除、到期触发与交付机制。

---

## 1. 设计决策（1B）

个人助理全面切换至 `agent-core` 后，已移除原 BE 侧的 `assistant_reminders` 独立表、自定义 `ReminderTools` 与 `AssistantReminderScheduler`，统一采用 `agent-core` 内置提醒引擎：

- **内置工具**：通过 `ASSISTANT_BUILTIN_TOOLS` 装配 `agent-core` 原生 `create_reminder`、`list_reminders` 与 `delete_reminder`，不再维护自研提醒工具。
- **持久化位置**：提醒定义与到期队列统一存储在 `agent-core` `records` 表对应助理 `tree_id` 作用域下（`key='reminders'` / `'reminder_queue'`）。
- **调度驱动**：由 `AssistantSchedulerRunner`（`app/service/assistant_runtime.py`）按 `min(heartbeat_poll_seconds, reminder_poll_seconds)` 周期遍历 `assistants` 中已启用助理，在 `user_scope(userid)` 跨实例锁保护下调用 `AssistantRuntimeService.tick_user(assistant, user=user)` 驱动 `agent-core` 的 `AgentScheduler.tick()`。

---

## 2. 到期执行与统一交付

1. `AgentScheduler.tick()` 将到期提醒物化并按 `source='reminder'` 提交给该助理的 `TreeAgent(execution_mode='queue')` 串行执行。
2. 模型根据提醒内容和当前上下文决定是否通知用户：
   - 需要通知时调用 `finish_task(send_to_user=True)`，依次经过 `AssistantMessageRenderingHook`（钉钉正文转换）与 `on_final_deliver` 回调，幂等写入 `assistant_deliveries` 并由 `AssistantDeliveryService.flush` 送达钉钉单聊；
   - 仅需内部记录或员工已离职时调用 `finish_task(send_to_user=False)`，不向用户发送消息。

---

## 3. 配置与验证状态

- 巡检周期受 `ASSISTANT_REMINDER_POLL_SECONDS` 与 `ASSISTANT_HEARTBEAT_POLL_SECONDS` 控制，随 `ASSISTANT_ENABLED=true` 一起启动。
- 验证状态：已在 `tests/assistant_v2/test_assistant_e2e.py` 中通过 SQLite + Mock DingTalk 验证到期提醒经 `AssistantSchedulerRunner.tick_once()` 触发、转换并交付的完整链路；尚未在真实钉钉环境执行线上收发验收。
