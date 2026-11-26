# 助理最终正文转换

本文档说明个人助理在向钉钉发送最终回复前的正文转换子智能体、钩子集成与统一交付链路。

---

## 1. 设计目标

个人助理面向钉钉单聊交付时，需将模型产出的标准 Markdown 转换为钉钉客户端兼容的排版格式，同时确保：

- 仅当 `finish_task` 的 `send_to_user=True` 且未触发心跳裁剪时才执行转换；
- 转换失败时阻止提交与发送，向主模型返回清晰失败原因与修正方向，绝不伪造已送达；
- 转换子智能体的调用明细通过 `context.record_generation` 写入私有审计，不泄露到用户正文。

---

## 2. 核心实现

1. **转换子智能体（`app/agent/assistant/message_rendering/agent.py`）**：
   - `DingTalkMessageRenderer` 基于 `agent_core.VLLMClient`、`agent_core.infrastructure.vllm_tokenizer.count_chat_tokens` 与 `agent_core.model.node_persistence.GenerationPayload` 实现，已彻底移除旧 `app.infrastructure.mllm` 与 `app.agent.core` 依赖。
   - `render(text, *, on_generation=None) -> str` 返回转换后的钉钉正文；异常统一包装为 `MessageRenderingError`。
2. **工具钩子（`app/agent/assistant/message_rendering/hook.py`）**：
   - `AssistantMessageRenderingHook` 实现 `agent_core.core.tool` 钩子协议；当 `execution.result.status == 'succeeded'` 且 `arguments.send_to_user` 为真时调用 `DingTalkMessageRenderer.render`，将转换结果写入 `execution.output`。
   - 转换失败抛出 `ToolOperationFailed`，由主模型据此调整正文或如实说明限制。
3. **统一交付与钩子顺序（`app/agent/assistant/tool/finish_task.py`）**：
   - `AssistantFinishTask` 继承 `agent_core.core.tool.base.FinishTaskTool`，在 `execute` 中直接创建 `after_commit` 回调（遵守 `agent-core` `ToolRegistry` 禁止钩子替换 `after_commit` 的不变量）。
   - 钩子链顺序：`AssistantMessageRenderingHook` → `capture_rendered_output`（原地记录转换后的最终 `output`）→ `FeedbackOnlyHook(render_assistant_finish)`。
   - `after_commit` 触发 `on_final_deliver(task_id, rendered_text, file_ids)`，幂等写入 `assistant_deliveries` 并调用 `AssistantDeliveryService.flush`，从而让中间件用户消息、1B 原生提醒与心跳任务共用同一套可靠交付链路。
