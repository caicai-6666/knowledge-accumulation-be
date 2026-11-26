# 用户附件引用与文件定位

本文档说明探索模式与个人助理共用的附件引用模型、磁盘定位规则及访问授权边界。

---

## 1. 数据模型与磁盘定位

`app/model/question_file.py` 定义稳定附件引用 `AttachmentReference` 与上传载荷 `UploadedFile`：

- `type`：`images`（对应 `.pdf`）、`audio`（`.mp3`）、`video`（`.mp4`）。
- `file_id`：服务端分配的 36 位小写带连字符 UUID（`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`）。
- 磁盘文件名固定为 `<file_id><extension>`，由 `UploadFileStore`（`app/infrastructure/upload_file.py`）在 `data/user/upload-file/` 下以目录描述符与 `O_NOFOLLOW` 独占读写，拒绝符号链接和父目录跳转。

---

## 2. 转换与登记流程

1. **探索模式上传**：`POST /exploration/trees/{tree_id}/turns` 接收原始文件（PDF、图片、Office 文档，单份 ≤ 10 MiB、合计 ≤ 20 MiB），经 `app/service/exploration_attachment.py` 调用外部 MCP `file_conversion` 统一转为 PDF 并用 PyMuPDF 校验，随后写入 `UploadFileStore` 并在 `exploration_tree_files` 中登记 `(tree_id, owner_userid, file_id, filename)`。
2. **个人助理入站与消费**：钉钉入站阶段由 `RobotInboundService` 经 `app/infrastructure/robot_attachment.py` 调用 MCP `file_conversion` 转 PDF 暂存并上传中间件队列（此时不登记 `uploaded_files`）；消费阶段由 `AssistantConsumer` 下载后按稳定 UUID 落盘，登记到 `uploaded_files`（默认 `private`）及 `assistant_tree_files`。

---

## 3. 授权与读取边界

- **HTTP 读取**：`GET /resource/attachment/{tree_id}/{file_id}` 由 `app/router/resource.py` 校验中央用户认证后，通过 `ExplorationTreeRepository.get_tree` 与 `has_file` 在读取前后双次核对归属，再从 `UploadFileStore` 返回 PDF 字节；UUID 本身不构成授权凭据。
- **模型阅读（`view_pdf`）**：
  - 探索模式通过 `ExplorationTreeRepository.has_file(tree_id, owner_userid, file_id)` 授权，并由 `get_file_name` 提供进度展示文件名。
  - 个人助理通过 `MySQLAssistantRepository.has_file(tree_id, owner_userid, file_id)` 并叠加 `FileVisibilityService` 的 `public` 可见性门禁授权，防止私密文件被越权读取。
- **删树清理**：删除探索树时，`ExplorationTreeService.delete_tree` 先终止运行中任务并清理数据库记录，再按返回的 `file_id` 列表逐一从 `UploadFileStore` 删除磁盘文件。
