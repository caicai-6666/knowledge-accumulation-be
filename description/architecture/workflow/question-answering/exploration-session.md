# 探索会话与 SSE 执行流

## 用途与范围

探索模式为前端提供问答树管理（创建、列表、改名、打开、删除）与「先注册、后由 SSE 订阅激活」的多轮问答任务流，支持主线续聊、历史节点持久化分叉、运行中取消与改向（`supersede`）以及断线重连回放。

- 树服务：`app/service/exploration_tree.py`
- 任务与 SSE 服务：`app/service/exploration_turn.py`
- 运行时服务：`app/service/exploration_runtime.py`
- 元数据仓库：`app/repository/exploration_tree.py`
- HTTP / SSE 路由：`app/router/exploration.py`、`app/router/exploration_turn.py`

---

## 核心流程

```mermaid
flowchart LR
    P[POST /turns<br/>deferred=True 注册] --> A[GET /turns/{id}/events<br/>首次订阅触发 job.activate()]
    A --> B[后台消费 job.events()<br/>缓冲最近 512 条事件]
    B --> C[SSE 白名单帧推送<br/>+ 15s 心跳]
    C --> D{客户端断开?}
    D -- 是 --> E[后台任务继续执行<br/>支持 Last-Event-ID 重连]
    D -- 否 --> F[终态事件结束流]
```

### 主干与分支语义（`advance_head`）

探索模式直接使用 `agent-core` 原生 `advance_head` 能力控制主干指针 `TreeView.head`：

- `mode='main'`：校验起点为当前 `view.head`，调用 `TreeAgent.generate(..., parent_id='latest', advance_head=True)` 推进主干。
- `mode='branch'`：允许从任意历史已结束节点持久化分叉（`workspace_retention='per_node'`），传入 `parent_id=selected_node_id, advance_head=False`，不改写主干 `view.head`。
- `POST /open`：调用只读 `runtime.tree(tree_id, include_display=True)` 获取 `TreeView`；若存在在途活跃节点 `view.active` 且该节点 `TreeNode.advance_head=True`，则以该活跃节点作为有效主干末尾 `effective_main_leaf`（否则取 `view.head`），沿 `parent_id` 回溯标注主干 `path_type='main'`，其余分叉标注 `path_type='branch'`，并推导 `children_ids` 与 `superseded_by_node_id`。

### 取消、改向与附件

- **取消**：`POST /turns/{turn_id}/cancel` 调用 `TreeAgent.stop(turn_id)`，等待收束至终态；重复取消幂等。
- **改向**：`POST /turns` 携带 `supersedes_turn_id` 时直接调用 `agent-core` 原生 `TreeAgent.redirect(..., deferred=True)`，旧任务立即中断收束为 `superseded`，新任务保持 `pending_activation` 等待前端订阅时 `activate()` 并自动继承旧任务的 `advance_head`。对在途旧任务的 `old_worker` 仅执行短时非取消等待（`0.3s`），避免旧任务正在执行跨平台 A2A 等长耗时工具时阻塞改向注册接口与树锁；若竞态下 `agent.redirect` 抛出 `StorageConflict`，清理本次未生效上传文件并返回 `409 Conflict`。
- **附件**：`multipart/form-data` 上传文件经 [附件统一转 PDF](../../../capability/application/attachment-conversion.md) 转换后写入 `UploadFileStore`，并登记到 `exploration_tree_files` 供 `view_pdf` 按当前用户与当前树鉴权；删除问答树时先停任务并 `close_tree`，再原子清理 `agent-core` 数据（含 `display:*`）、业务表记录与磁盘附件。

---

## SSE 事件映射与断线恢复

后台消费 `SessionTask.events()` 并将 `agent-core` 事件映射为前端白名单 SSE 事件：

| `agent-core` 事件 | 前端 SSE 事件 | 说明 |
| --- | --- | --- |
| `task.started` / `task.completed` / `task.cancelled` / `task.superseded` / `task.failed` | `turn.status` | 同步任务生命周期状态。 |
| `progress` | `task.progress` | 透传工具或思考进度提示（`type`、`message`）。 |
| `message.delta` | `message.delta` | 中途与最终候选正文增量预览。 |
| `message.completed` | `message.completed` | 权威提交确认或预览中断。 |
| 异常错误 | `error` | 不暴露底层堆栈或工程追踪标识。 |

每个活跃任务在内存中保留最近 **512 条**带递增 ID 的 SSE 帧，支持通过 `Last-Event-ID` 断线重连回放，空闲期间每 15 秒发送保活心跳；客户端关闭连接不会取消后台任务，任务终态后也可通过 `GET /turns/{turn_id}` 查询快照。

### 节点展示历史与耗时（`agent-core` 原生承载）

- 任务起止时间与耗时（`started_at`、`finished_at`、`duration_ms`）及节点展示历史（`records` 中 `display:<task_id>` + `AgentEvent.sequence`）均由 `agent-core` 原生先落库再推 SSE。
- 实时 SSE 订阅注册时不插入未激活伪事件，激活后按 `AgentEvent.sequence` 与 `TreeNode.display.last_sequence` 严格对齐；终态 SSE 优先读取 `TreeNode` 的权威耗时字段，使实时展示与随后 `POST /open` 的毫秒值一致。
- `POST /open` 通过 `runtime.tree(tree_id, include_display=True)` 获取 `node.display` 并投影为 `event_source='recorded'` 的前端白名单 `TaskDisplayHistory`（无 `display` 记录时回退 `event_source='legacy'`）。

---

## 验证状态

- 自动化测试：`tests/exploration_v2/test_exploration_e2e.py` 覆盖建树、列表、改名、主线流式 SSE 与 `Last-Event-ID` 回放、历史节点 `branch` 分叉后 `main_leaf_id` 保持不变、取消、改向、附件上传与删树清理，全部通过。
- 真实容器联调：已在独立测试容器中验证完整 HTTP/SSE 链路及新旧契约一致。
