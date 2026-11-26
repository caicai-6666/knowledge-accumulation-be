# 工具内容与反馈契约

## 用途与范围

本项目全部模型可见工具均基于 `agent-core` 的 `RegisteredTool`、`ToolExecution`、`ToolExecutionResult`、`FoldableToolContent` 与 `FeedbackOnlyHook` 契约实现，遵循[工具反馈渲染规范](../../../standard/tool-feedback.md)，严格分离程序消费的结构化 `tool_result` 与模型阅读的中文展示正文。

---

## 核心契约

| 组件 | `agent-core` 位置 | 职责 |
| --- | --- | --- |
| `RegisteredTool` | `agent_core` | 声明工具名、说明、Pydantic 参数模型、执行器、`return_types`、`page_resolver`、`progress`/`progress_factory`、`read_pools`/`write_pools` 与 `hooks`。 |
| `check_descriptions` | `agent_core.core.tool.registry` | 注册时递归检查工具 JSON Schema 所有字段（含嵌套对象、`items`、`anyOf`、`additionalProperties`）均具备非空 `description`。 |
| `ToolProgress` | `agent_core.core.tool.progress` | 白名单进度类型（`thinking`/`online-search`/`external-expert`/`local-search`/`a2a`/`boardcast`），文案以「正在」开头且 ≤2000 字符。 |
| `FoldableToolContent` | `agent_core.core.tool.result` | 可折叠资料页（`ToolPageReference`）与三态轻量回执（`visible`/`hidden`/`unavailable`），按需通过 `page_resolver` 返回文本或 `image_url`。 |
| `FeedbackOnlyHook` | `agent_core` | 位于每个工具 `hooks` 链末尾的纯展示钩子；校验不可更改 `status`、`failure_mode`、`tool_result`、`output` 与 `after_commit`。 |

> **关键不变量：** 业务钩子与渲染钩子均不得替换 `execution.after_commit`；需要提交后回调的工具（如 `AssistantFinishTask`、`send_assistant_message`、可折叠查询池）必须在 `execute` 中创建 `after_commit`。

---

## 全量工具清单

### `agent-core` 内置工具

- 探索模式（8 个）：`workspace_add`、`workspace_replace`、`workspace_delete`、`compact_context`、`emit_progress`、`finish_task`、`search_memory`、`view_memory_query`。
- 个人助理（9 个）：`workspace_add`、`workspace_replace`、`workspace_delete`、`compact_context`、`search_memory`、`view_memory_query`、`create_reminder`、`list_reminders`、`delete_reminder`（`finish_task` 由助理定制覆盖）。

### MCP 二次封装工具（7 个，探索与助理共用）

`get_current_time`、`calculate_date`、`search_web`、`view_web_search_results`、`open_web_page`、`ask_external_expert`、`continue_external_expert`（实现见 `app/agent/exploration/mcp_tools.py`）。

### 本地共享工具（至多 6 个，探索与助理共用）

`search_files`、`view_file_candidates`、`view_pdf`、`list_sub_management_platforms`、可选 `broadcast_platform_a2a` / `continue_platform_a2a`（实现见 `app/agent/exploration/tools/`）。

### 个人助理专属工具（16 个）

`finish_task`（定制 `AssistantFinishTask`）、`get_file_visibility`、`set_file_visibility`、`add_file_note`、`delete_file_note`、`search_file_notes`、`view_file_note_query`、`check_file_disclosure`、`search_portraits`、`view_portrait_query`、`add_portrait`、`delete_portrait`、`search_assistants`、`view_assistant_query`、`send_assistant_message`、`search_calendar_events`、`view_calendar_query`、`search_memory_window`（实现见 `app/agent/assistant/tool/`）。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_mcp_tools.py`、`tests/exploration_v2/test_tools.py`、`tests/assistant_v2/test_assistant_tools.py` 逐项验证全部工具通过 `ToolRegistry` / `check_descriptions`、末尾挂接 `FeedbackOnlyHook` 且输出不含原始 JSON。
