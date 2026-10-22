# 探索模式问答树 API

为 exploration 模式提供已认证用户的问答树创建、列表、改名、完整打开、删除及两阶段问答任务。仅处理 exploration 类型。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [获取问答树列表](#获取问答树列表) | `GET` | `/knowledge-accumulation/api/exploration/trees` |
| [创建问答树](#创建问答树) | `POST` | `/knowledge-accumulation/api/exploration/trees` |
| [修改问答树名称](#修改问答树名称) | `PATCH` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/name` |
| [打开问答树](#打开问答树) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/open` |
| [删除问答树](#删除问答树) | `DELETE` | `/knowledge-accumulation/api/exploration/trees/{tree_id}` |
| [注册探索任务](#注册探索任务) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns` |
| [订阅并激活探索任务](#订阅并激活探索任务) | `GET` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/events` |
| [获取探索任务快照](#获取探索任务快照) | `GET` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}` |
| [停止探索任务](#停止探索任务) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/cancel` |

树路由定义位于 `app/router/exploration.py`，问答任务路由位于 `app/router/exploration_turn.py`，由 `app/main.py` 挂载；树 ID 复用[问答树模型](../architecture/data/question-tree.md)的 32 位小写十六进制约束，列表、改名与打开复用中央身份认证及现有 MySQL 配置，问答树仓库由应用生命周期管理，首次查询才连接数据库，停机关闭连接池；不自动创建表。

---

## 获取问答树列表

`GET /knowledge-accumulation/api/exploration/trees`：获取当前用户的全部探索问答树，仅返回树 ID 和名称。

- **认证方式**：必填 header `Authorization: Bearer <access_token>`，使用显式登录签发的中央用户 token。每次请求查询中央会话，不隐式登录或恢复失效 token。
- **请求参数**：无 query 参数、无请求体；传入任何 query 或非空请求体返回 `422`。不接受客户端传入 userid 或树类型。
- **成功响应**：`200 application/json`，对象数组，每项只有 `id`（32 位小写十六进制字符串）和 `name`（树名称）；没有探索树返回 `[]`。附带 `Cache-Control: no-store`。

```json
[
  {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "技术方案比较"}
]
```

**错误响应**：

| 状态 | 条件 | 响应 |
| --- | --- | --- |
| `401` | Bearer 缺失、不合法或中央用户会话失效 | `{"detail":"用户 token 无效、过期或中央登录已离线"}` |
| `422` | 携带额外查询参数或请求体 | `{"detail":"请求参数不合法"}` |
| `503` | 认证依赖不可用 | 脱敏 `detail`，遵循[身份查询契约](auth.md#查询当前身份) |
| `503` | 数据库故障、超时或仓库未装配 | `{"detail":"问答树列表暂不可用"}` |

错误响应同样附带 `Cache-Control: no-store`；认证依赖故障不作为用户失效处理，数据库故障不降级为空列表。

请求示例（替换用户 token）：

```bash
curl -i http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees \
  -H 'Authorization: Bearer <access_token>'
```

查询仅使用认证所得的 userid，并在 SQL 中固定筛选 `kind='exploration'`；排除 `assistant` 个人助理树及其他用户的所有树。按 `updated_at DESC, id DESC` 排序，当前返回全部匹配项、不分页；只读取树表的 `id/name`，不加载节点、正文或工作区，不要求管理员或文件访问等级。

---

## 创建问答树

`POST /knowledge-accumulation/api/exploration/trees`：创建本人空 exploration 树，用于“开始新探索”。不创建任务、上传附件或执行模型。

- **认证方式**：必填 `Authorization: Bearer <access_token>`，使用中央随机用户 token；不要求管理员或文件等级。
- **请求参数**：必须提交 JSON 对象；唯一字段 `name` 可省略（默认本次请求的北京时间，格式 `YYYY-MM-DD HH:mm:ss`），指定时须为去首尾空白后 1–200 字符的字符串。无 query；禁止客户端传入 `id/userid/kind` 等其他字段。
- **成功响应**：`201 application/json`、`Cache-Control: no-store`；仅返回服务端生成的 32 位小写十六进制 `id` 与实际保存的 `name`。

```json
{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"2026-10-22 12:34:56"}
```

**错误响应**：`401` 为中央身份无效；`422` 为请求体缺失、名称或其他参数非法；`503` 为认证依赖或存储故障，创建失败返回脱敏 `{"detail":"问答树创建暂不可用"}`。空对象 `{}` 合法，无请求体不合法。

```bash
curl -X POST http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees \
  -H 'Authorization: Bearer <access_token>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"方案比较"}'
```

创建后用返回的 ID 注册首问，省略 `selected_node_id`、指定 `mode=main`，收到 turn_id 后再订阅激活；也可先 open 获取标准空树响应。树的归属来自当前身份，kind 固定 exploration。每次成功创建生成独立 ID；无幂等键，创建超时或返回不确定时不能自动重试，可先刷新列表核对。任务注册接口不会自动创建不存在的客户端 tree_id。

---

## 修改问答树名称

`PATCH /knowledge-accumulation/api/exploration/trees/{tree_id}/name`：修改本人探索问答树的名称。

- **认证方式**：必填 header `Authorization: Bearer <access_token>`，每次查询中央会话，不要求管理员或文件访问等级。
- **请求参数**：path `tree_id` 必填，32 位小写十六进制字符串；不接受 query 参数。JSON 请求体仅允许 `name`，必填字符串，去除首尾空白后为 1–200 个字符，拒绝空白名称和额外字段。

```json
{"name": "新的方案名称"}
```

**成功响应**：`200 application/json`，只返回 `id/name`，名称为实际保存的去除首尾空白后的值：

```json
{"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "新的方案名称"}
```

**错误响应**：

| 状态 | 条件 | 响应 |
| --- | --- | --- |
| `401` | 缺少有效用户 Bearer 或中央登录失效 | `{"detail":"用户 token 无效、过期或中央登录已离线"}` |
| `404` | 树不存在、属于其他用户或为 assistant 类型 | `{"detail":"问答树不存在"}` |
| `422` | 树 ID、请求体或查询参数不合法 | `{"detail":"请求参数不合法"}` |
| `503` | 认证依赖不可用 | 脱敏 `detail`，遵循[身份查询契约](auth.md#查询当前身份) |
| `503` | 数据库故障、超时或仓库未装配 | `{"detail":"问答树改名暂不可用"}` |

成功及错误响应均附带 `Cache-Control: no-store`。

请求示例（替换树 ID 和用户 token）：

```bash
curl -i -X PATCH http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/name \
  -H 'Authorization: Bearer <access_token>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"新的方案名称"}'
```

在同一事务中按认证用户锁定树记录、确认 exploration 类型，再修改 `name`、递增 `revision` 并更新 `updated_at`。不修改节点、附件或工作区。客户端无需传版本号；并发改名按行锁获取顺序执行，后执行的名称生效。重复提交当前名称不变更版本和活动时间；改为不同名称后，列表排序使用更新后的活动时间。

---

## 打开问答树

`POST /knowledge-accumulation/api/exploration/trees/{tree_id}/open`：将本人 exploration 问答树加载到进程内，返回整棵树的拓扑与公开历史。

- **认证方式**：必填 header `Authorization: Bearer <access_token>`，每次查询中央会话；不要求管理员或文件访问等级，不隐式登录。
- **请求参数**：path `tree_id` 必填，32 位小写十六进制字符串；无 query 参数、无请求体，多余输入返回 `422`。
- **成功响应**：`200 application/json`，返回 `ExplorationTreeResponse`；不分页，不按最近摘要截断，不自动启动生成。成功和错误响应均附带 `Cache-Control: no-store`。

树字段：

| 字段 | 类型与含义 |
| --- | --- |
| `id`、`name` | 树 ID 与名称 |
| `kind` | 固定 `exploration` |
| `root_id` | 根节点 ID，空树为 `null` |
| `main_leaf_id` | 主干末端任务 ID，空树为 `null` |
| `revision` | 本次读取的数据库版本 |
| `created_at`、`updated_at` | 带时区的 UTC 时间 |
| `nodes` | 按 `sequence` 升序的完整节点数组，含所有主干、分支和摘要；空树为 `[]` |

节点采用平铺数组，前端可通过 `parent_id`、`children_ids` 构建可视化树，避免深层嵌套 JSON 的递归限制：

| 节点字段 | 含义 |
| --- | --- |
| `id`、`parent_id`、`children_ids` | 当前 ID、父 ID（根为 `null`）、按序排列的子节点 ID 数组 |
| `kind` | `task` 或 `summary` |
| `path_type` | `main` 主干或 `branch` 分支 |
| `status` | `generating`、`complete`、`cancelled`、`superseded`、`failed` |
| `sequence` | 树内节点顺序，不替代父子关系 |
| `created_at`、`finished_at` | 创建与结束时间，结束时间可为空 |
| `superseded_by_node_id` | 调整方向后的接替节点 ID，仅 superseded 节点非空 |
| `context_status` | cancelled 为 `user_manually_stopped`，superseded 为 `user_goal_adjusted`，其他为 `null` |
| `can_interrupt` | 当前是否为生成中的任务节点 |
| `processing_duration_ms` | 已记录结束时间时的耗时，否则为 `null` |
| `content` | task：`kind/question/attachments/answer`；summary：`kind/text/covered_from_id/covered_to_id` |
| `messages` | 节点公开消息数组，按节点内 sequence 排序，每项含 `id/kind/status/text/sequence/created_at` |
| `display` | 任务的持久化展示过程，字段见下文；摘要节点为 `null` |

附件仅返回 `type/file_id`。公开消息 kind 为 `question/intermediate/final`，status 为 `completed/interrupted`；保留中断输出的真实状态，不伪造最终答案。摘要节点的 messages 为空；历史上只保存正文、未写消息表的节点也可返回空消息数组，问题与答案仍以 content 展示。消息序号可能因穿插私有生成而不连续。

任务的 `display` 沿用参考项目的精简展示历史，保留以下字段：

| 字段 | 含义 |
| --- | --- |
| `events` | 按原始 SSE 序号排序的 `{sequence,event,data}`，仅含 `turn.status/task.progress/message.completed/error`；`data` 与实时 SSE 一致。 |
| `last_sequence` | 已确认的 SSE 游标，包含过滤的 delta，所以事件序号可不连续；旧记录为 `null`。 |
| `streaming_messages` | 当前为 `[]`，Core 只发布已验收的完整公开输出，没有待恢复的逐字半成品。 |
| `event_source` | `recorded` 为真实事件记录；`legacy` 只从已有公开输出恢复 `message.completed`，事件 sequence 为 `null`，不补造动作、错误或状态变化。 |

例如一次真实工具动作对应的历史事件（仅展示 `display` 中的一项）：

```json
{
  "sequence": 4,
  "event": "task.progress",
  "data": {
    "turn_id": "11111111111111111111111111111111",
    "type": "online-search",
    "message": "正在搜索网页：申请条件"
  }
}
```

前端以 `content` 展示输入、以 `display.events` 恢复过程；`messages` 保留已有消息读取契约，不能再与事件重复追加。历史中的动作只代表当时发生的操作，当前是否结束以节点 `status` 为准。摘要节点不具有任务展示过程。

部署前须手工执行 [010-question-node-display.sql](../../schema/010-question-node-display.sql)，应用不自动执行迁移。本次未修改真实数据库；旧记录不会补造此前未保存的动作。进程重启后可重新打开树读取展示历史，但不会恢复旧 SSE 连接或自动执行遗留任务。

**创建模式**：推进（`mode=main`）生成主干节点，探索（`mode=branch`）生成分支节点。当前主干末尾同时允许推进和探索，历史主干或分支节点只允许探索。前端结合 `path_type` 与 `main_leaf_id` 判断模式，不增加“允许开启分支”字段；生成中的任务不能作为继续提问起点，摘要是内部节点而非用户任务入口。

响应不包含 owner_userid、工作区正文/版本引用、工具交互 exchanges、原生推理或模型响应。空树响应示例：

```json
{
  "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "name": "技术方案比较",
  "kind": "exploration",
  "root_id": null,
  "main_leaf_id": null,
  "revision": 0,
  "created_at": "2026-10-22T00:00:00Z",
  "updated_at": "2026-10-22T00:00:00Z",
  "nodes": []
}
```

**错误响应**：

| 状态 | 条件 | 响应 |
| --- | --- | --- |
| `401` | 用户 Bearer 缺失、不合法或中央会话失效 | `{"detail":"用户 token 无效、过期或中央登录已离线"}` |
| `404` | 树不存在、属于其他用户或为 assistant 类型 | `{"detail":"问答树不存在"}` |
| `422` | 树 ID 非法、带 query 或非空请求体 | `{"detail":"请求参数不合法"}` |
| `503` | 认证依赖不可用 | 脱敏 `detail`，遵循[身份查询契约](auth.md#查询当前身份) |
| `503` | 加载超时、数据库故障、正文缺失或结构损坏 | `{"detail":"问答树打开暂不可用"}` |

请求示例（替换树 ID 和用户 token）：

```bash
curl -i -X POST http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/open \
  -H 'Authorization: Bearer <access_token>'
```

每次打开都重新确认数据库归属与用途，并在同一 REPEATABLE READ 事务中批量读取完整结构、正文及公开消息，验证成功后才驻留。失败不返回旧缓存或不完整树。打开本身不更新活动时间、节点状态或工作区；内存快照的刷新、容量与停机行为见[内存构树与正文加载](../architecture/data/question-tree.md#内存构树与正文加载)。

---

## 删除问答树

`DELETE /knowledge-accumulation/api/exploration/trees/{tree_id}`：删除本人的 exploration 树、全部关联数据库记录及该树已登记的上传附件，并释放当前进程内的树快照。

- **认证方式**：必填 header `Authorization: Bearer <access_token>`，每次查询中央登录；不要求管理员或文件访问等级，不隐式登录。
- **请求参数**：path `tree_id` 必填，32 位小写十六进制字符串；无 query 参数、无请求体。不接受客户端传入 userid 或文件路径。
- **成功响应**：数据库记录、附件及清理清单均删除后返回 `204 No Content`，响应体为空。成功与错误均附带 `Cache-Control: no-store`。

**错误响应**：

| 状态 | 条件 | 响应 |
| --- | --- | --- |
| `401` | 用户 token 缺失、不合法或中央登录失效 | `{"detail":"用户 token 无效、过期或中央登录已离线"}` |
| `404` | 树不存在、属于其他用户、为 assistant 类型，且无本人待完成的清理清单 | `{"detail":"问答树不存在"}` |
| `409` | 同 UUID 登记类型冲突，或发现其他树引用该附件 | `{"detail":"附件登记存在冲突，暂不能删除"}` |
| `422` | 树 ID 非法、携带 query 参数或非空请求体 | `{"detail":"请求参数不合法"}` |
| `503` | 认证依赖不可用 | 脱敏 `detail`，遵循[身份查询契约](auth.md#查询当前身份) |
| `503` | 数据库失败、超时、迁移缺失，或附件清理未完成 | `{"detail":"问答树删除尚未完成，请重试"}` |

请求示例（替换树 ID 和用户 token）：

```bash
curl -i -X DELETE http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  -H 'Authorization: Bearer <access_token>'
```

数据库删除和文件系统没有共同事务：数据库阶段失败会回滚且不删除附件；数据库已经提交、文件清理失败时，树已从列表和打开入口消失，但待清理的文件清单仍在数据库。客户端应重试相同 DELETE，服务端按本人清单继续清理，已缺失文件按已完成处理。全部清理成功后移除清单；之后再次删除返回 `404`，不伪造另一份成功记录。

部署前须手工执行 [009-question-tree-deletion.sql](../../schema/009-question-tree-deletion.sql)，应用不自动建表，本轮没有修改真实数据库。完整数据范围、安全边界与并发约束见[整树删除与附件清理](../architecture/data/question-tree.md#整树删除与附件清理)。不新增定时扫描任务；重启后同一 DELETE 仍可恢复清理。

---

## 注册探索任务

`POST /knowledge-accumulation/api/exploration/trees/{tree_id}/turns`

创建本树的待激活任务，成功 `201`。先返回任务 ID，首次订阅 SSE 才执行。普通新任务只暂存输入；改变方向时先原子登记接替关系及附件，但不执行新任务模型。

**认证**：`Authorization: Bearer <access_token>`，中央验证当前用户；仅本人 exploration 树。不接受 query 参数。

**请求**：`multipart/form-data`，严格限定下列字段，不接受重复普通字段。

| 参数 | 必填 | 内容 |
| --- | --- | --- |
| path `tree_id` | 是 | 32 位小写十六进制树 ID |
| `selected_node_id` | 普通非空树任务必填 | 本树任务节点 ID；空树首轮省略。改变方向时省略或与 `supersedes_turn_id` 相同，不传字符串 `null` |
| `mode` | 是 | `main` 推进模式；`branch` 探索模式 |
| `question` | 是 | 1–20000 字符，不能全空白 |
| `files` | 否 | 重复此字段直接上传 PDF；最多 10 份，每份 10 MiB，总量 20 MiB |
| `thinking_effort` | 是 | `low`、`medium`、`xhigh`，对应低、中、高三档 |
| `supersedes_turn_id` | 否 | 改变方向时指定同树尚未结束的旧任务 ID；不传则是普通新任务 |

推进模式（`main`）只允许从当前主干末尾继续，生成新的主干节点；该起点也可选择探索模式（`branch`），生成分支且不改变主干末尾。历史主干或分支节点只能探索，不能升级主干。创建模式 `mode` 与节点类型 `path_type` 含义不同，接口值均保留 `main/branch`。摘要/生成中节点不能作为起点。空树首轮只能推进。同一树只允许一个待激活或执行中的任务；已有任务时须显式传 `supersedes_turn_id` 才能改变方向。

```json
{
  "tree_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "turn_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "status": "pending_activation",
  "can_interrupt": true,
  "activation_expires_at": "2026-10-22T08:03:00Z"
}
```

`turn_id` 也是任务节点 ID；普通任务的 pending 阶段还没有 SQL 节点。替代已落库旧任务时，为保证关联外键和事务完整，新节点会在注册阶段创建，但仍不执行模型。响应另含 `supersedes_turn_id`，普通任务为 null。响应 `Cache-Control: no-store`。PDF 文件后缀、数量与大小在注册阶段验收，实际内容损坏、加密或无页在激活后以 SSE 失败报告；不会静默当作图片处理。

```bash
curl -X POST 'http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees/<tree_id>/turns' \
  -H 'Authorization: Bearer <access_token>' \
  -F 'selected_node_id=<node_id>' \
  -F 'mode=branch' \
  -F 'question=请结合附件比较两种方案' \
  -F 'thinking_effort=medium' \
  -F 'files=@材料.pdf;type=application/pdf'
```

错误：`401` 中央登录失效；`404` 树不存在、非本人或 assistant 树；`409` 节点/模式冲突或已有活跃任务；`413` 上传超限或 multipart 格式非法；`422` 输入、文件名、空文件不合法；`503` 依赖故障或暂存容量满。错误只返回脱敏 `detail`。


**改变方向的调用方式**：仍使用本注册接口，增加 `supersedes_turn_id=<旧 turn_id>`，提交新的 question、thinking_effort 和可选 PDF；mode 必须与旧任务一致。不要先调用 cancel，否则旧任务已成为 cancelled 终态，不能再替代。

```bash
curl -X POST 'http://127.0.0.1:24000/knowledge-accumulation/api/exploration/trees/<tree_id>/turns' \
  -H 'Authorization: Bearer <access_token>' \
  -F 'supersedes_turn_id=<old_turn_id>' \
  -F 'mode=main' \
  -F 'question=先暂停原来的比较，改为评估实施成本' \
  -F 'thinking_effort=medium'
```

注册成功即停止旧执行，旧 SSE 收到 `superseded` 终态；响应的新 turn_id 仍是 `pending_activation`，前端订阅其 events 才开始执行。新旧快照分别提供 `supersedes_turn_id` 和 `superseded_by_turn_id`。旧任务已提交的公开输出、工作区和完整私有审计保留，模型通过“用户调整方向”理解新问题，不混同为“用户终止”。

- 替代已落库执行任务：SQL 原子创建直接子任务并标记旧节点 superseded；继承旧节点的主干/分支属性，不通过改变方向切换路径。
- 替代尚未激活、没有 SQL 节点的任务：只替换内存待激活输入，从旧任务原先选择的节点开始，旧 pending 输入不会执行或补造历史节点。
- 正在准备上下文、尚未落库的 processing 任务暂不支持改变方向，返回 `409`，可等待 `node_id` 就绪；单纯停止仍可使用 cancel。`can_interrupt` 表示可停止，不保证当前已具备可替代的 SQL 节点。
- 非法输入/PDF、容量不足、归属错误或事务回滚不停止旧任务。旧最终提交先赢时返回 `409`，不改写 complete 历史。上传内容在方向调整注册阶段提前验收，损坏 PDF 返回 `422`。
- 新任务取消或过期不会恢复旧任务。已登记接替节点取消落为 cancelled；过期/停机落为 failed，HTTP 过期仍返回 expired/410。若数据库清理失败返回 `503` 或后台脱敏重试，不假称节点已清理。

---

## 订阅并激活探索任务

`GET /knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/events`

携带中央用户 Bearer，无 query/body。可选 `Last-Event-ID: <sequence>`，默认 0；必须是最多 20 位 ASCII 非负整数。首次订阅在归属、游标和最新树状态校验通过后激活一次，随后订阅只回放和追踪同一任务。无需再调用独立 activate API。

成功 `200 text/event-stream`，`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`。示例：

```text
id: 1
event: turn.status
data: {"turn_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","status":"pending_activation","can_interrupt":true,"context_status":null,"created_at":"2026-10-22T08:00:00+00:00","activated_at":null,"finished_at":null,"processing_duration_ms":null,"node_id":null,"summary_id":null}

id: 2
event: turn.status
data: {"turn_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","status":"processing","can_interrupt":true,"context_status":null,"created_at":"2026-10-22T08:00:01+00:00","activated_at":"2026-10-22T08:00:01+00:00","finished_at":null,"processing_duration_ms":null,"node_id":null,"summary_id":null}

: heartbeat
```

任务节点落库后另发一次 `processing`，填写 `node_id` 和可选 `summary_id`。工具动作通过 `task.progress` 展示，例如 `{"turn_id":"<turn_id>","type":"online-search","message":"正在搜索网页：官方 | 申请条件"}`。该事件只说明动作，不能作为工具成功结论；不写入公开历史消息，终态后不再发布。事件可按现有序号回放，任务快照的 `progress` 保存最近动作；精简历史另持久化到节点 `display`。公开输出与动作事件如下：

| event | data 主要字段 |
| --- | --- |
| `turn.status` | `turn_id/status/can_interrupt/context_status/created_at/activated_at/finished_at/processing_duration_ms/node_id/summary_id/supersedes_turn_id/superseded_by_turn_id` |
| `task.progress` | `turn_id/type/message`；type 为 `thinking`（思考）、`online-search`（网页检索/读取）或 `external-expert`（专家咨询/追问）；基础工具默认声明 `thinking`，每个工具执行前发布其注册状态，结束恢复 `thinking`；message 为不超过 2000 字符的动作提示 |
| `message.delta` | `turn_id/message_id/message_kind/delta`；kind 为 `intermediate/final` |
| `message.completed` | `turn_id/message_id/message_kind/text/status`，status 为 `completed` |
| `error` | `turn_id/code/message/retryable`，脱敏错误 |

输出在 Core 校验并落库后发送：当前一条完整公开输出对应一次 delta 和一次 completed，**不是模型原生逐 token 推理流**。前端追加 delta，completed 的 text 覆盖该消息正文。消息 ID 与打开树的历史一致；最终答复消息之后仍须以任务 `completed` 状态判断整轮结束。终态还包括 `cancelled/superseded/failed/expired`；`superseded` 的 `context_status=user_goal_adjusted`，并用 `superseded_by_turn_id` 指向新任务。

网络断开不取消执行。使用 `Last-Event-ID` 重连，恢复时只发送更大序号事件；不能把重连当新任务创建。前端需使用支持 Authorization header 的 fetch 流或 SSE 客户端。

连接前错误：`401/404/409/410/422/503`。`409` 回放不足时先获取任务快照；`410` 超过 180 秒首次激活期限，需重新注册。连接开始后发生回放失效或树被删除，发送无 `id` 的连接级 `error` 并结束，不修改任务状态。模型执行失败则发送有序号 `error` 和 `failed`。展示历史写入失败时停止任务，并发送脱敏 `history_unavailable` 错误及失败通知；这些故障通知可能未落库，打开树只能返回最后确认的事件前缀，须同时检查节点状态与已落库消息。

---

## 获取探索任务快照

`GET /knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}`

Bearer 认证，无 query/body。返回 `200 application/json`、`Cache-Control: no-store`，用于刷新页面或回放窗口不足后的展示恢复；不会激活任务。

字段：`tree_id/turn_id/node_id/summary_id/supersedes_turn_id/superseded_by_turn_id/status/can_interrupt/context_status/activation_expires_at/activated_at/finished_at/processing_duration_ms/last_sequence/earliest_sequence/messages/progress/error`。`messages` 是完整公开消息数组，每项 `message_id/message_kind/text/status`；没有原生推理或工具轨迹。使用快照替换界面后，以 `last_sequence` 订阅 SSE，不重复追加快照内容。`node_id` 在尚未登记节点时为 null。`progress` 为最近一次 `{type,message}`，没有动作时为 null；终态保留它，但不表示仍在执行。

任务与事件在当前进程驻留：终态保留 1800 秒；进程重启或回收后返回 `404`，已落库历史通过[打开问答树](#打开问答树)取得。错误 `401/404/422/503`，不从内存绕过中央用户认证。

---

## 停止探索任务

`POST /knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/cancel`

Bearer 认证，无 query/body。待激活任务直接取消并释放暂存字节；执行中取消等待生产协程和持久化清理，不删除已登记附件。成功 `200` 返回与快照接口相同结构，`Cache-Control: no-store`。

取消终态为 `cancelled`，`context_status=user_manually_stopped`。重复取消幂等；已完成、被替代、失败或过期返回 `409`。若取消期间最终答案已经提交，以 SQL 完成状态为准，不把已完成节点改成取消。数据库状态无法确认时不保证取消落库，以快照和重新打开树核对。

错误 `401/404/409/422/503`。删除整棵树会先停止该树活跃任务，再执行数据库和附件清理。具体生命周期与部署限制见[探索任务注册与 SSE 执行](../architecture/workflow/question-answering/exploration-session.md)。
