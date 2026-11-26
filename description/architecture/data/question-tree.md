# 探索树与 agent-core 存储契约

本文档说明探索问答树与个人助理共用的 `agent-core` 持久化表结构，以及探索模式专用的轻量业务元数据表。

---

## 1. 职责与分层

- **`agent-core` 底层存储**：由独立库 `agent-core` 提供的 `MySQLStorage` / `SQLiteStorage` 维护任务节点（含 `started_at`、`finished_at`、`duration_ms`）、主干指针 `head`（由 `advance_head` 控制）、节点展示历史（`records` 中 `display:<task_id>`）、上下文摘要、工作区、祖先记忆向量、全文索引及内置定时提醒，命名空间固定为 `scope = json.dumps(["agent-core:trees", tree_id], ensure_ascii=False, separators=(",", ":"))`。
- **探索业务元数据表**：由 `app/repository/exploration_tree.py`（`ExplorationTreeRepository`）维护树的归属、名称、更新时间与树内附件清单，补充 `agent-core` 不承担的多用户隔离、列表排序、改名与删树清理语义。
- **共存隔离**：不再使用历史 `question_*` 表；所有新空库统一按 `agent_core.schema_sql("mysql")` 与探索/助理业务 DDL 初始化。

---

## 2. agent-core 核心表（5 张）

建表 SQL 由 `agent_core.schema_sql("mysql" | "sqlite")` 提供，`MySQLStorage.connect(pool)` 与 `SQLiteStorage.connect(path)` 在连接时校验表结构与版本号（`SCHEMA_VERSION = 1`）。

| 表名 | 核心列 | 用途 |
| --- | --- | --- |
| `records` | `scope, key, version, value(JSON)` | 乐观锁 KV 存储：保存树头状态（`tree`，含主干 `head`）、任务节点（`task:<id>`，含起止时间与耗时）、展示历史（`display:<id>`）、工作区、任务排队、审计与 1B 内置提醒（`reminders` / `reminder_queue`）。 |
| `spaces` | `scope, space_id, dimensions` | 记录每棵树绑定的向量空间标识与维度，防止混用不同 Embedding 模型的向量。 |
| `memory` | `scope, task_id, field, source_id, text, vector, source_hash` | 持久化祖先任务记忆片段及向量，供 `search_memory` / `view_memory_query` 检索（SQLite 配套 `memory_fts`）。 |
| `file_notes` | `scope, file_id, name, summary, updated_at` | `agent-core` 内置文件备忘表（MySQL 使用 ngram FULLTEXT，SQLite 配套 `file_notes_fts`）；本项目个人助理保留独立业务表 `assistant_file_notes`。 |
| `agent_core_schema` | `id=1, version` | 记录 `agent-core` Schema 版本号。 |

---

## 3. 探索业务元数据表（2 张）

`app/repository/exploration_tree.py` 通过 `exploration_schema_sql("mysql" | "sqlite")` 导出合并 DDL（`agent-core` 5 表 + 以下 2 张业务表），并提供 `init_sqlite_schema` / `init_mysql_schema` 供测试与容器初始化使用；生产应用启动时不自动建表。

### 3.1 `exploration_trees`

| 字段 | 类型（MySQL / SQLite） | 约束与说明 |
| --- | --- | --- |
| `tree_id` | `VARCHAR(32)` / `TEXT` | 主键，32 位小写十六进制 UUID。 |
| `owner_userid` | `VARCHAR(128)` / `TEXT` | 树归属员工 `userid`，所有查询和修改均强校验归属。 |
| `name` | `VARCHAR(200)` / `TEXT` | 去除首尾空白后的树名称（1–200 字符）。 |
| `created_at` | `VARCHAR(64)` / `TEXT` | ISO-8601 UTC 时间字符串（微秒精度）。 |
| `updated_at` | `VARCHAR(64)` / `TEXT` | 最后一次持久化更新的 ISO-8601 UTC 时间，用于树列表倒序排列。 |

> **主干指针与展示历史由 `agent-core` 原生托管：** 探索模式提交主线任务时传入 `advance_head=True`，分叉任务传入 `advance_head=False`，`agent-core` 将该标记持久化并透传至 `TreeNode.advance_head`；已结束主干推进 `TreeView.head`，在途活跃节点生成期间通过 `view.active` 与 `TreeNode.advance_head` 保持有效主干投影不误判为分支。节点起止耗时（`started_at`/`finished_at`/`duration_ms`）与展示历史（`display:<task_id>`）均由 `agent-core` 原生落盘，宿主不再维护额外展示表或冗余主干列。

### 3.2 `exploration_tree_files`

| 字段 | 类型（MySQL / SQLite） | 约束与说明 |
| --- | --- | --- |
| `tree_id` | `VARCHAR(32)` / `TEXT` | 联合主键之一，所属探索树 ID。 |
| `owner_userid` | `VARCHAR(128)` / `TEXT` | 所属员工 `userid`。 |
| `file_id` | `VARCHAR(64)` / `TEXT` | 联合主键之一，`UploadFileStore` 分配的小写带连字符 UUID。 |
| `filename` | `VARCHAR(512)` / `TEXT` | 上传原始文件名，供 `view_pdf` 进度提示展示。 |
| `created_at` | `VARCHAR(64)` / `TEXT` | ISO-8601 UTC 登记时间。 |

---

## 4. 关键操作与删树清理

`ExplorationTreeRepository` 对外提供：

- `create_tree` / `list_trees` / `get_tree` / `rename_tree` / `touch_tree`
- `register_files` / `has_file` / `get_file_name`
- `delete_tree_data(tree_id, owner_userid) -> list[str]`：校验归属后，先清理 `records`（含 `task:*` 与 `display:*`）、`spaces`、`memory`、`file_notes`（SQLite 同步清理 `memory_fts`、`file_notes_fts`）中对应 `scope` 的行，再删除 `exploration_tree_files` 与 `exploration_trees`，并返回待删磁盘 `file_id` 列表交由 `ExplorationTreeService` 清理 `UploadFileStore`。

---

## 5. 部署待办

- 跨仓库 `knowledge-accumulation-deploy` 的 `mysql/init/001-schema.sql` 需同步切换为 `agent-core` 5 表 + `exploration_trees` + `exploration_tree_files` + 个人助理业务表（见[个人助理存储契约](assistant-storage.md)）。
