# Agent Core 运行时接入

## 用途与范围

本项目已全面采用独立封装的 `agent-core` 库承载智能体主循环、上下文预算与压缩、工作区管理、祖先记忆检索与向量化、PDF 逐页渲染及内置心跳/定时提醒调度，替代原有自维护的 `app/agent/core/` 循环与本地向量/记忆实现。

- 探索运行时装配：`app/service/exploration_runtime.py`
- 个人助理运行时装配：`app/service/assistant_runtime.py`
- 本地工具与提示词：`app/agent/exploration/tools/`、`app/agent/assistant/`

---

## 核心组件与接入方式

### 模型与存储装配

两种模式共享 `agent-core` 提供的模型与存储抽象：

| 组件 | `agent-core` 符号 | 说明 |
| --- | --- | --- |
| 对话模型客户端 | `VLLMClient` | 复用本项目 `.env` 中的 `VLLM_MLLM_*` 配置（`app/config/mllm.py`）。 |
| 向量模型客户端 | `VLLMEmbedding` | 复用 `EMBEDDING_*` 配置（`app/config/embedding.py`），供内置记忆检索与向量化使用。 |
| 服务端分词计数 | `vllm_counter` | 统一用于上下文预算与压缩验收。 |
| 数据库存储 | `MySQLStorage` / `SQLiteStorage` | 生产走 `MySQLStorage.connect(pool)`（复用 `asyncmy` 连接池），自动化测试走 `SQLiteStorage.connect(path)` 隔离运行。 |
| 建表契约 | `schema_sql("mysql" \| "sqlite")` | 生成 `agent-core` 所需的 5 张核心表：`records`、`spaces`、`memory`、`file_notes`、`agent_core_schema`。 |

### 探索模式与个人助理配置差异

| 维度 | 探索模式（Exploration） | 个人助理（Assistant） |
| --- | --- | --- |
| `execution_mode` | `interruptible`（支持取消与改向 `redirect`） | `queue`（串行排队执行） |
| `workspace_retention` | `per_node`（保留每节点工作区，支持历史节点持久化分叉） | `latest`（单份滚动工作区） |
| `guidance_factory` | `ProgressGuidance.factory(interval_seconds=60.0)`（开场与周期 `emit_progress` 提醒） | 无周期中途播报 |
| `prompt_options` | `build_exploration_prompt_options()`（默认 `'auto'` 工作区与压缩装配，通过 `PromptOptions(output_guidance=...)` 注入探索中途反馈与收敛细则） | `build_assistant_prompt_options()`（默认 `'auto'` 工作区与压缩装配，通过 `PromptOptions(output_guidance=...)` 注入个人助理输出与交付细则） |
| 内置工具 | `workspace_add/replace/delete`、`compact_context`、`emit_progress`、`finish_task`、`search_memory`、`view_memory_query` | `workspace_add/replace/delete`、`compact_context`、`search_memory`、`view_memory_query`、`create_reminder`、`list_reminders`、`delete_reminder` + 定制 `finish_task` |
| 心跳与提醒 | 不启用 | 启用 `AgentScheduler`（1B 方案：内置到期提醒 + 空转心跳原生 `_prune()` 裁剪） |

### 工具运行时与钩子机制

- **静态工具上下文**：`search_files` 等复合工具在执行期通过 `current_tool_runtime()` 读取主任务的 `core`、`journal` 与 `thinking_effort`，无需为每轮任务重建子图工具。
- **缓存池隔离**：`CachePool`（`scope='task'` 或 `scope='session'`）与 `current_tool_cache` 统一管理记忆结果集、文件检索结果集与 PDF 文档 LRU，提交前暂存、提交后生效。
- **末尾纯展示钩子**：所有工具在钩子链末尾挂载 `FeedbackOnlyHook`，保证展示层不改写 `status`、`failure_mode`、`tool_result`、`output` 与 `after_commit`。

---

## 数据边界与部署待办

`agent-core` 仅按 `scope` 存取核心表（含主干 `head`、`advance_head` 控制、节点起止耗时、`display:<task_id>` 展示历史与 `redirect(..., deferred=True)`），不维护业务侧树列表、归属用户、改名或上传附件索引。宿主侧通过以下业务表补齐：

- 探索模式：`exploration_trees`、`exploration_tree_files`（`app/repository/exploration_tree.py`）。
- 个人助理：`assistants`、`assistant_task_executions`、`assistant_deliveries`、`assistant_tree_files`、`assistant_portraits*`、`assistant_file_notes`、`uploaded_files`、`assistant_robot_inbox`。

> **部署待办（跨仓库）：** `knowledge-accumulation-deploy` 仓库的 `mysql/init/001-schema.sql` 需更新为初始化 `agent-core` 5 张核心表（InnoDB + `utf8mb4_bin` + ngram FULLTEXT）及上述业务表；原有 `question_*` 旧表不再使用。

---

## 验证状态

- 自动化测试：`tests/exploration_v2/` 与 `tests/assistant_v2/` 全套单测与 SQLite 端到端测试已全部通过。
- 容器联调：已在独立测试容器与独立 MySQL 测试库中验证探索模式全链路（含建表、建树、流式 SSE、MCP 工具调用、分叉与删树清理）。个人助理未执行真实钉钉凭据收发。
