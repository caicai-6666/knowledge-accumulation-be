# 跨平台 A2A 协作

## 用途与范围

探索模式与个人助理在启用跨平台协作开关时，装配 `broadcast_platform_a2a` 与 `continue_platform_a2a` 两个工具，通过中间件平台 A2A 队列向其他子管理平台广播求助、单聊续聊或继续等待超时单聊。该能力默认关闭，开启后须提供 `tree_id` 与运行中的 `PlatformA2AService`。

- 工具实现：`app/agent/exploration/tools/platform_tools.py`（`build_platform_a2a_tools`）
- 调度与账本服务：`app/service/platform_a2a.py`（`PlatformA2AService`）
- 中间件客户端与模型：`app/infrastructure/platform_a2a.py`、`app/model/platform_a2a.py`

---

## 工具契约与核心行为

| 工具名 | 用途 | 关键校验与进度 |
| --- | --- | --- |
| `broadcast_platform_a2a` | 向 1 至 100 个子管理平台开启新话题并等待回复 | 目标代码去重并经目录校验；进度 `ToolProgress(type='boardcast', message='正在广播求助')`。 |
| `continue_platform_a2a` | 按已有 `response_id` 发送追问（`action="chat"`）或仅继续等待超时单聊（`action="wait"`） | `chat` 必填 `message`、`wait` 禁止 `message`（违反抛 `ModelActionRejected`）；动态 `progress_factory` 展示目标平台名称（`type='a2a'`）。 |

关键设计：

1. **可信身份与幂等调用**：`user_id`、`tree_id` 由宿主构造时固定绑定，`node_id` 优先从 `current_tool_runtime().journal` 解析，`call_id` 取自 `agent_core.core.tool.registry.current_tool_call_id`。
2. **提交确认**：协作成功完成后通过 `after_commit` 调用 `service.repository.accept(identity)` 确认接纳，后续才允许围绕该 `response_id` 发起续聊或继续等待。
3. **不确定状态保护**：`PlatformA2AError(uncertain=True)` 或 `publish_unknown` 映射为 `status='unknown'`，主循环立即停止，不自动重发。
4. **末尾渲染钩子**：挂载 `FeedbackOnlyHook(render_a2a_feedback)`，展示按平台分组的状态、正文与续聊/续等 `response_id`，无原始 JSON 外壳。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_tools.py` 覆盖参数校验、动态进度、成功提交确认、`unknown`/`failed`/`correctable` 失败分支及 `ToolRegistry` Schema 校验，全部通过。
- 真实跨平台收发：尚未在真实多平台环境中执行端到端收发验证。
