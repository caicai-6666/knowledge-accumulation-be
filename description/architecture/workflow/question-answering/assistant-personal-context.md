# 员工个人信息与助理私有画像

本文档说明个人助理的员工背景注入、同事目录检索以及私有画像增删查的实现边界。

---

## 1. 员工背景注入

- `AssistantConsumer` 在执行队列任务前，通过 `AssistantEmployeeContextLoader`（`app/service/assistant_personal_context.py`）加载所属员工的可信档案（在职状态、部门、管理平台等），与平台访问等级及跨助理来源说明一并拼入 `supplemental` 注入 `TreeAgent.generate`。
- 探索模式不注入个人档案或私有画像，保持与个人助理的上下文隔离。

---

## 2. 私有画像与同事检索工具

以下工具均已迁移至 `agent-core` 契约，使用本地轻量分页池 `app/agent/assistant/tool/_query_pool.py`（零 `app.agent.core` 依赖），末尾均挂接 `FeedbackOnlyHook` 渲染中文正文：

| 工具名 | 源码位置 | 职责 |
| --- | --- | --- |
| `search_assistants` / `view_assistant_query` | `app/agent/assistant/tool/{search_assistants,view_assistant_query}.py` | 检索本项目在职员工与助理标识并分页查看。 |
| `search_portraits` / `view_portrait_query` | `app/agent/assistant/tool/portrait_query.py` | 按员工姓名全文检索当前助理私有的同事偏好画像，按人分组分页展示。 |
| `add_portrait` / `delete_portrait` | `app/agent/assistant/tool/portrait_mutation.py` | 凭 `agent-core` 的 `current_tool_call_id` 直接调用 `AssistantPortraitRepository.add_idempotent` / `delete_idempotent` 幂等落库（写入 `assistant_portraits` 与 `assistant_portrait_actions` 去重账本）。 |

---

## 3. 隔离与安全约束

- 画像严格按 `owner_userid` 隔离，任何助理只能读写所属用户自己的私有画像，不能跨用户查询或修改。
- 新增与删除均基于 `(task_id, operation_hash)` 唯一约束幂等去重，不再依赖旧检查点 `commit_hook`。
