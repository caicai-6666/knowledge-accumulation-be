# 钉钉消息入站与附件转换

本文档说明钉钉单聊机器人入站消息的去重、附件经 MCP 转 PDF、引用复用及顺序发布到中间件队列的流程。

---

## 1. 入站主流程

`RobotInboundService`（`app/service/robot_inbound.py`）接收 `DingTalkRobotStream` 推送的可信单聊消息：

1. **去重与入站账本**：在 `RobotInboxRepository`（`assistant_robot_inbox` 表，`robot-inbox` + `robot-user` 命名锁）中按 `request_id` 幂等登记，保留 `arrival_sequence` 保证同用户消息严格按到达顺序发布。
2. **首次建档**：若发送方为尚未建档的真实员工，通过 `EmployeeOnboardingService` 补齐员工档案与头像后再继续处理。
3. **引用消息复用**：`RobotReplyFileResolver`（`app/service/robot_reply_file.py`）通过 `MySQLAssistantRepository.find_message_content` / `find_message_attachment` 与 `find_file` 核对当前助理树中已存在的附件，命中且通过 `validate_pdf` 时直接复用，不重复下载转码。
4. **附件异步转 PDF（MCP）**：
   - `app/infrastructure/robot_attachment.py` 提供 `to_pdf_async(part, content, mcp_client, *, max_bytes)`，调用外部 `MCPClientManager.convert_file(..., operation='auto')` 将图片及 Office 文档统一转为 PDF，并经 `validate_pdf`（复用 `agent_core.infrastructure.pdf_lock.serialized_pdf_operation` + PyMuPDF）校验。
   - **错误分层**：`converter_unavailable` / `font_unavailable` / `converter_busy` / `conversion_timeout` / 传输不可用或未装配 MCP 客户端 → 抛出 `AttachmentConversionUnavailable`（不拒收、可重试）；不支持格式、损坏/加密或体积超限 → 抛出 `AttachmentRejected`（整条拒收并回复原因）。
   - **附件引用渲染**：使用 `agent_core.core.context_rendering.attachment.render_attachments`。
5. **发布边界**：入站阶段仅暂存转换后的 PDF 并随队列任务上传中间件，**不在入站阶段登记 `uploaded_files`**；真正登记发生在 `AssistantConsumer` 消费落盘阶段。

---

## 2. 依赖与验证状态

- 依赖外部 `knowledge-accumulation-mcp-server` 提供 `file_conversion` 工具，不再依赖本地 LibreOffice 或已删除的 `app/infrastructure/file_conversion/`。
- 验证状态：`tests/assistant_v2/test_robot_attachment.py` 已覆盖 `to_pdf_async` 正常转换、体积超限、错误分层与 `RobotInboundService` 集成（Mock MCP）；真实钉钉机器人入站收发未在本次迁移后重新执行线上验收。
