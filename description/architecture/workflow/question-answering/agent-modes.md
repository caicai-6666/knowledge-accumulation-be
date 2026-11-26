# 问答模式与装配

## 用途与范围

本项目在统一的 `agent-core` `AgentRuntime` / `TreeAgent` 之上装配两种业务模式：**探索模式**面向 Web 前端多轮交互与持久化分支探索；**个人助理**面向钉钉单聊、跨助理协作、到期提醒与空闲心跳。两种模式均彻底移除旧 `app/agent/core/` 依赖，提示词完全自包含。

- 探索模式入口：`app/service/exploration_runtime.py`、`app/agent/exploration/tools/`
- 个人助理入口：`app/service/assistant_runtime.py`、`app/agent/assistant/factory.py`

---

## 模式差异对照

| 维度 | 探索模式（Exploration） | 个人助理（Assistant） |
| --- | --- | --- |
| 运行模式（`execution_mode`） | `interruptible`（支持取消与改向） | `queue`（按用户树串行消费） |
| 工作区保留（`workspace_retention`） | `per_node`（每节点独立快照，支持任意历史节点分叉） | `latest`（单份持续演进工作区） |
| 角色提示词（`role`） | `build_exploration_role_prompt()`（知更/小知角色、公司背景与多平台协作） | `build_assistant_role_prompt()`（忠实条款、文件告知规则、公司背景、多平台协作与个人信息/画像规则） |
| 系统提示词选项（`prompt_options`） | `build_exploration_prompt_options()`（通过 `PromptOptions(output_guidance=...)` 将 `emit_progress` 主动中途反馈规则注入 `## 用户输出与任务收敛`） | `build_assistant_prompt_options()`（通过 `PromptOptions(output_guidance=...)` 将个人助理 `send_to_user` 与交付规则注入 `## 用户输出与任务收敛`） |
| 周期进展提醒 | `build_exploration_guidance_factory(interval_seconds=60.0)`（`ProgressGuidance.factory`） | 不启用 |
| 单轮附加上下文 | `build_exploration_supplemental(user)`（`### 平台访问等级`） | 用户身份、在职状态、平台等级、个人背景与来源消息元数据 |
| 心跳与定时提醒 | 不启用 | `agent-core` 内置 `AgentScheduler`（1B：内置提醒工具 + 空转心跳原生 `_prune()` 裁剪） |
| 最终输出 | `agent-core` 内置 `finish_task` + `emit_progress` | 定制 `AssistantFinishTask`（在职门禁 + `AssistantMessageRenderingHook` 钉钉排版转换 + `on_final_deliver` 统一交付） |

---

## 工具装配矩阵

| 工具来源 | 探索模式 | 个人助理 |
| --- | --- | --- |
| `agent-core` 内置（8 / 9 个） | `workspace_add/replace/delete`、`compact_context`、`emit_progress`、`finish_task`、`search_memory`、`view_memory_query` | `workspace_add/replace/delete`、`compact_context`、`search_memory`、`view_memory_query`、`create_reminder`、`list_reminders`、`delete_reminder`（`finish_task` 由助理定制替换） |
| MCP 通用工具（7 个） | `get_current_time`、`calculate_date`、`search_web`、`view_web_search_results`、`open_web_page`、`ask_external_expert`、`continue_external_expert` | 完整复用 |
| Phase 1 本地工具（至多 6 个） | `search_files`、`view_file_candidates`、`view_pdf`、`list_sub_management_platforms`、可选 `broadcast_platform_a2a`/`continue_platform_a2a` | 完整复用，且本地 `view_pdf` 叠加 `wrap_image_authorize` 的 `public` 可见性门禁 |
| 助理专属工具 | 无 | `finish_task`（定制）、`get/set_file_visibility`、`add/delete/search_file_notes`、`view_file_note_query`、`check_file_disclosure`、`search/view_portrait*`、`add/delete_portrait`、`search_assistants`、`view_assistant_query`、`send_assistant_message`、`search_calendar_events`、`view_calendar_query`、`search_memory_window` |

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_tools.py`、`tests/assistant_v2/test_assistant_tools.py` 验证全部工具通过 `ToolRegistry` 递归 Schema 校验、末尾挂载 `FeedbackOnlyHook`，且助理定制 `finish_task` 通过 `supports_heartbeat_pruning`。
- 端到端测试：`tests/exploration_v2/test_exploration_e2e.py` 与 `tests/assistant_v2/test_assistant_e2e.py` 在隔离 SQLite 环境下全部通过。
