# 个人助理存储契约

本文档说明个人助理在 `agent-core` 底层存储之外保留的 MySQL 业务表与仓库职责。

---

## 1. 架构分工

- **对话树、工作区、记忆向量与 1B 定时提醒**：全部交由 `agent-core` 的 5 张核心表（`records`、`spaces`、`memory`、`file_notes`、`agent_core_schema`）托管，见[探索树与 agent-core 存储契约](question-tree.md)。
- **共享数据库连接与锁**：`app/repository/mysql_base.py` 提供 `MySQLRepositoryBase`，统一维护 `asyncmy` 连接池、`_transaction()`（REPEATABLE READ）与基于独立连接的 `GET_LOCK` 命名锁（`assistant` / `robot-inbox` / `robot-user`，附带 1 秒 ping 保活监控）。
- **个人助理业务表**：由 `MySQLAssistantRepository`（`app/repository/assistant_runtime.py`）、`AssistantPortraitRepository`、`AssistantFileNoteRepository`、`UploadedFileRepository`、`RobotInboxRepository` 与 `PlatformA2ARepository` 维护，已彻底解除对旧 `question_trees` 外键、`assistant_workspaces` 与旧 `assistant_reminders` 表的依赖。

---

## 2. 业务表清单

| 表名 | 所属仓库 | 主要职责 |
| --- | --- | --- |
| `assistants` | `MySQLAssistantRepository` | 维护每位员工唯一的 `tree_id`（32 位十六进制）与 `enabled` 开关；`ensure(userid)` 直接分配 `tree_id`，不再依赖 `question_trees` 外键。 |
| `assistant_task_executions` | `MySQLAssistantRepository` | 记录中间件队列任务领取、租约（`delivery_token` / `lease_expires_at`）、附件映射（`file_bindings`）、执行状态（`received` / `prepared` / `running` / `completed` / `failed`）与 `acknowledged_at`。 |
| `assistant_deliveries` | `MySQLAssistantRepository` | 交付 Outbox：按 `(task_id, action_key)` 幂等记录发往钉钉用户（`final` / `file:<uuid>`）或跨助理队列（`call:<sha256>`）的发送动作、尝试次数、未知结果标记与外部回执。 |
| `assistant_tree_files` | `MySQLAssistantRepository` | 登记助理树内已落盘附件 `(tree_id, owner_userid, file_id, file_type, created_at)`，供 `view_pdf`、`AssistantFileDeliveryService` 与 `RobotReplyFileResolver` 核对归属（同时兼容回查 `assistant_task_executions.file_bindings`）。 |
| `assistant_portraits` / `assistant_portrait_sequence` / `assistant_portrait_actions` | `AssistantPortraitRepository` | 存储私有画像、自增数字 key 序列及按 `(task_id, operation_hash)` 幂等去重的动作账本（提供 `add_idempotent` / `delete_idempotent` 供工具直写）。 |
| `uploaded_files` / `assistant_file_notes` | `UploadedFileRepository` / `AssistantFileNoteRepository` | 上传文件可见性（`private` / `public`）与 2A 个人文件备忘（见[上传文件元信息与个人备忘](assistant-file-notes.md)）。 |
| `assistant_robot_inbox` | `RobotInboxRepository` | 钉钉机器人入站消息去重、顺序发布与取消账本。 |
| `platform_a2a_calls` | `PlatformA2ARepository` | 跨平台 A2A 广播与续聊私有账本。 |

---

## 3. 关键设计边界

1. **外部网络调用在事务之外**：数据库事务仅完成状态推进与 Outbox 落库，钉钉消息发送和中间件队列发布均在事务外执行，发送前先标记 `delivery_unknown` 防止进程中断后盲目重发。
2. **1B 提醒与心跳不再占用独立业务表**：定时提醒与空闲心跳状态均保存在 `agent-core` `records` 表对应助理 `tree_id` 作用域下；`MySQLAssistantRepository.list_enabled_assistants()` 仅向 `AssistantSchedulerRunner` 提供已启用助理列表。
3. **部署待办**：跨仓库 `knowledge-accumulation-deploy` 的 `mysql/init/001-schema.sql` 需补充 `assistant_tree_files` 建表，并移除 `assistants` 对旧 `question_trees` 的外键约束及已废弃的旧 `question_*` / `assistant_workspaces` / `assistant_reminders` 表。
