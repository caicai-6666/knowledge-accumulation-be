# 附件统一转 PDF

## 用途与范围

探索模式 HTTP 上传附件与钉钉单聊机器人入站附件均统一转换为 PDF 后落盘至 `UploadFileStore`（语义类型 `images`，磁盘后缀 `.pdf`），供 `view_pdf` 工具按页渲染图像阅读。

本地 LibreOffice / 字体包转换已移除，全部格式转换统一委托外部 MCP Server 的 `file_conversion` 工具执行，详见[MCP Server 接入](../infrastructure/mcp-server.md)。

- 探索上传转换：`app/service/exploration_attachment.py`
- 钉钉机器人附件转换：`app/infrastructure/robot_attachment.py`
- MCP 文件转换客户端：`app/infrastructure/mcp_client.py`（`MCPClientManager.convert_file`、`ConversionError`、`ConvertedPDF`）

---

## 支持格式与约束

- 支持图片、PDF、Word（`.doc`/`.docx`）、Excel（`.xls`/`.xlsx`）及 PowerPoint（`.ppt`/`.pptx`）。
- 探索上传保留单文件与总大小上限校验（`MAX_FILE_BYTES`、`MAX_TOTAL_BYTES`），超限抛 `UploadSizeExceeded`。
- 转换后的 PDF 经轻量 PyMuPDF `validate_pdf` 校验（复用 `agent_core.infrastructure.pdf_lock.serialized_pdf_operation` 串行化 PDF 解析），确保文件可读且非空页后才落盘。

---

## 错误分层边界

### 探索上传（`prepare_uploads_async`）

- 转换失败抛出 `ConversionError(code, reason)`，由路由层按错误语义返回客户端错误或服务暂不可用，不落盘半成品文件。

### 钉钉机器人入站（`to_pdf_async`）

严格区分可重试的临时故障与应整条拒收的附件错误：

| 远端或本地错误 | 映射异常 | 入站处理行为 |
| --- | --- | --- |
| `converter_unavailable`、`font_unavailable`、`converter_busy`、`conversion_timeout`、MCP 传输不可用或未装配 | `AttachmentConversionUnavailable` | 不拒收消息，允许上游重试。 |
| 不支持的格式、加密/损坏文件、超限或其他校验失败 | `AttachmentRejected` | 整条入站消息拒收并回执原因，不写入队列或 `uploaded_files`。 |

`uploaded_files` 登记仍在消费侧准备附件时完成，入站阶段不提前登记。

---

## 验证状态

- 单元测试：`tests/assistant_v2/test_robot_attachment.py`（覆盖错误分层、PyMuPDF 校验与异步转换）与 `tests/exploration_v2/test_exploration_e2e.py`（附件上传 → 转 PDF → `view_pdf` 阅读）已通过。
