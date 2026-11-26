# 注册中心文件检索

## 用途与范围

探索模式与个人助理共用 `search_files` 与 `view_file_candidates` 工具检索注册中心正式文件，按当前可信员工权限返回文件候选列表（含注册中心文件 ID、来源平台代码、来源平台文件 ID 与通行证），供后续 `view_pdf` 阅读原文或跨平台 A2A 协作引用。

- 后端适配器与工具装配：`app/agent/exploration/tools/file_search.py`
- 权威检索服务：`app/service/file_retrieval.py`
- 注册中心只读仓库：`app/repository/registered_file.py`
- 内存检索索引：`app/infrastructure/file_search_index.py`

---

## 与 agent-core 的桥接设计

`RegistryFileSearchBackend` 实现 `agent_core.FileSearchBackend` 协议，在构造时固定绑定可信 `user_id` 与可选 `platforms` 白名单，不接受模型参数改写身份：

| `agent-core` 检索字段 | `FileRetrievalService` 字段 | 说明 |
| --- | --- | --- |
| `name` | `name` | 文件名称检索。 |
| `summary` | `abstract` | 摘要全文与向量混合检索（RRF 融合，依赖 `VLLMEmbedding`）。 |
| `tag` | `passport` | 跨平台通行证精确匹配，不做 Top-K 截断。 |

`read(file_ids)` 回读注册中心权威快照并构造 `FileSearchRecord`，在 `metadata` 中保留：

- `来源平台代码`（`row.platform`）
- `文件权限等级`（`row.level`）
- `来源平台文件 ID`（`row.source_id`，存在时保留）
- `tags` 承载 `row.passport`

`build_exploration_file_search_tools(...)` 直接调用 `agent_core.build_file_search_registration(backend=...)` 与 `build_file_candidates_registration(backend=...)`，省略 `core`/`journal`/`thinking_effort`；执行期由 `agent-core` 通过 `current_tool_runtime()` 自动复用主任务的模型、推理档位与私有审计日志。

---

## 权限与一致性边界

- `RegisteredFileRepository.read` 在单次 `REPEATABLE READ` 只读快照事务中同时读取 `employees.platform_permissions` 与 `resources`，仅返回当前用户满足 `file_level <= permission_level` 的记录。
- `FileRetrievalService.search` 在排序返回前再次回查最新权威记录，若期间权限撤销、删除或改名则拒绝发布陈旧候选。
- 单次检索结束后立即释放内存正文与词频，仅保留有界向量 LRU 缓存。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_tools.py` 覆盖 `RegistryFileSearchBackend` 字段映射、空范围短路、metadata 保留及 `ToolRegistry` Schema 校验，全部通过。
