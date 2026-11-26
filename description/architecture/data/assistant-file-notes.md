# 上传文件元信息与个人备忘

本文档说明个人助理附件的上传元信息（`uploaded_files`）与个人文件备忘（`assistant_file_notes`）的数据契约及工具接入现状。

---

## 1. 架构定位（2A 方案）

个人助理在迁移至 `agent-core` 后，保留 BE 侧独立的 `uploaded_files` 与 `assistant_file_notes` 两张业务表，不与 `agent-core` 内置 `file_notes` 混用，以完整保留：

- 文件可见范围控制（`private` / `public`），并与助理侧 `view_pdf` 阅读门禁联动；
- 原始上传时间（`uploaded_at`）与备忘更新时间（`updated_at`）双时间维度筛选；
- 名称与摘要独立中文全文索引（MySQL `ngram` FULLTEXT）及 `and` / `or` 组合检索。

---

## 2. 数据契约

### 2.1 `uploaded_files`

由 `UploadedFileRepository`（`app/repository/uploaded_file.py`）基于 `MySQLRepositoryBase` 维护：

- 核心字段：`id`（36 位 UUID 主键）、`owner_userid`、`type`（`images` / `audio` / `video`）、`visibility`（默认 `private`，可由所属用户助理改为 `public`）、`uploaded_at`、`updated_at`。
- 幂等登记：`AssistantConsumer` 在消费队列附件并落盘至 `UploadFileStore` 后，通过 `UploadedFileRegistrationService.register` 按原消息接收时间登记；重复登记不覆盖已有 `visibility` 或 `uploaded_at`。
- 权限隔离：`get_owned` 与 `set_visibility` 严格校验 `owner_userid`，拒绝跨用户查询或修改。

### 2.2 `assistant_file_notes`

由 `AssistantFileNoteRepository`（`app/repository/assistant_file_note.py`）基于 `MySQLRepositoryBase` 维护：

- 核心字段：`owner_userid`、`file_id`（外键关联 `uploaded_files.id`）、`name`、`summary`、`updated_at`；每位用户对同一文件最多一条备忘。
- 写入约束：仅允许为所属用户本人已登记在 `uploaded_files` 中的文件新增备忘，已存在时不静默覆盖；删除仅限定当前 `owner_userid`。
- 全文检索：`search` 支持按 `file_ids` 二次筛选、`name_keyword` / `summary_keyword` 独立全文匹配（`and` / `or`），以及按 `uploaded_at` 或 `updated_at` 时间区间过滤。

---

## 3. 已接入的助理工具

以下工具均已迁移至 `agent-core` 契约，并由 `app/agent/assistant/factory.py`（`build_assistant_tools`）统一装配，末尾均挂接 `FeedbackOnlyHook` 输出中文 Markdown 正文：

- 可见范围管理：`get_file_visibility`、`set_file_visibility`（`app/agent/assistant/tool/file_visibility.py`）
- 备忘增删：`add_file_note`、`delete_file_note`（`app/agent/assistant/tool/file_note.py`）
- 备忘检索与分页查看：`search_file_notes`、`view_file_note_query`（`app/agent/assistant/tool/file_note_query.py`，复用本地 `_query_pool.py` 分页池）
