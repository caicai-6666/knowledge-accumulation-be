# 工具失败分层处理

## 用途与范围

本文档说明本项目在 `agent-core` 工具执行链路中对失败状态与失败模式的分层约定，覆盖本地工具、MCP 二次封装工具及个人助理业务钩子。规范总则见[工具反馈渲染规范](../../../standard/tool-feedback.md#失败分层与主循环)。

---

## 分层矩阵

`ToolExecutionResult` 通过 `status`（`succeeded` / `failed` / `unknown`）与 `failure_mode`（`correctable` / `failed` / `dependency`）共同决定主循环行为：

| 组合 | 典型场景 | 主循环行为 |
| --- | --- | --- |
| `status='failed', failure_mode='correctable'` | Schema/参数错误（`ModelActionRejected`）、越界页码、无效/过期 `query_id`、单文件无权访问、助理私密附件拦截、MCP 参数/会话可修正错误 | 进入临时纠错轨；连续达到上限停止；下次有效成功后清除整段纠错轨迹并保留私有审计。 |
| `status='failed', failure_mode='failed'` | 注册中心权限校验/下载不可用、平台目录故障、MCP 远端搜索/专家执行失败、日历/画像/备忘查询依赖故障、`ToolOperationFailed`（如钉钉正文转换失败） | 保留失败回执供模型说明限制或换策略，不提交候选工作区或 `after_commit`，不计入参数纠错计数。 |
| `status='failed', failure_mode='dependency'` | 会话/缓存池已关闭、MCP 传输不可用（`MCPUnavailableError`）、缺少可信 `current_tool_call_id` | 立即停止本轮任务，不自动重放。 |
| `status='unknown'` | 跨平台 A2A 发布结果不明（`publish_unknown`）、文件可见性/备忘/跨助理交付写入结果未确认 | 立即停止，不声称成功或未执行，禁止自动重放副作用。 |

---

## 业务钩子失败通道

对于尚未提交业务动作的前置业务钩子（当前用于 `AssistantMessageRenderingHook` 钉钉正文排版转换），遇到格式验收或请求失败时抛出 `agent_core.core.tool.result.ToolOperationFailed(reason=..., required_action=...)`：

- `ToolRegistry` 清除候选输出与提交回调，跳过后续业务钩子，仅执行末尾 `FeedbackOnlyHook` 渲染可读失败正文，并将结果作为 `status='failed', failure_mode='failed'` 交给主循环继续处理。
- 审计写入失败等不可恢复异常仍抛出 `AgentDependencyError`，直接终止任务。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_mcp_tools.py`、`tests/exploration_v2/test_tools.py` 与 `tests/assistant_v2/test_assistant_tools.py` 覆盖 `correctable`/`failed`/`dependency`/`unknown` 及 `ToolOperationFailed` 全部路径并全部通过。
