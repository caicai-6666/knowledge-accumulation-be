# 探索任务注册与 SSE 执行

## 职责与入口

探索问答复用 contract-service-be 的两阶段交互：先注册任务并返回 `turn_id`，前端首次订阅该任务 SSE 才激活执行。路由在 `app/router/exploration_turn.py`，输入与公开快照在 `app/schema/exploration_turn.py`，进程内任务、回放和生命周期在 `app/service/exploration_turn.py`。请求路径和示例见[探索 API](../../../api/exploration.md#注册探索任务)。

新探索先通过[创建问答树](../../../api/exploration.md#创建问答树)获得服务端 ID，再注册首问；也可延续本人已有 exploration 树，不开放 assistant 创建。归属和平台等级均取中央认证所得身份，不接受客户端指定用户、权限或模型角色。创建、订阅、快照、取消请求分别校验中央 token；已建立的连接和后台任务不周期查询中央，下一次 HTTP 请求重新验证。

---

## 输入与模式

树 ID 放在 path；multipart 接收所选任务节点 `selected_node_id`、`mode`、`question`、PDF `files`、三档 `thinking_effort`（`low/medium/xhigh`）。用户问题必填且不能全空白；不新增补充说明字段。

| 起点 | 允许的模式 |
| --- | --- |
| 空树，省略节点 | 推进（`main`），创建首个主干任务 |
| 当前主干末尾任务 | 推进（`main`）生成主干，或探索（`branch`）生成分支 |
| 历史主干任务 | 仅探索（`branch`），保留已有主干父链 |
| 分支任务 | 仅探索（`branch`），后续仍为分支 |
| 摘要、生成中节点、其他树节点 | 拒绝 |

创建模式称为推进/探索，节点类型仍称主干/分支；探索不改变 `main_leaf_id`，原主干末尾仍可继续推进。已有父链和 `main_leaf_id` 决定主干身份，不由前端标签改写。内部 `QuestionAgentService.generate` 新增 `advance_main` 和 `expected_revision` 参数，将显式选择带到摘要、任务创建的同一 SQL 事务；未提供参数的既有内部调用保持原规则。同树暂时只允许一个待激活或执行任务，避免不同生成检查点争用全树 revision；不同树可以并行。

上下文仍从所选节点向根回溯至最近摘要，继承该节点工作区。若上下文超限，生成新摘要后再创建任务，不使用所选节点未来的摘要。任务 ID 预先分配，也是激活后任务节点 ID；摘要节点使用另一个 ID。

---

## 注册、激活与存储

1. 创建接口完成中央身份、树归属、节点与模式校验，读取有界上传字节；普通注册只在内存暂存输入，不调用模型、不写 PDF 或任务节点；改变方向的事务例外见下文。
2. 首次 SSE 订阅检查身份、回放游标、树版本及节点，原子切换 `pending_activation → processing` 并启动唯一生产协程。此时重取认证身份中的平台等级。
3. 后台校验真实 PDF 内容：必须是可打开、未加密且至少一页的 PDF；之后才保存文件、加载上下文并运行 Core。失败发送脱敏 `error` 与 `failed` 终态，不假装完成。
4. 文件使用 UUID 与 `.pdf` 后缀，登记为 `type=images`；原始上传文件名不用作磁盘路径或模型标识。沿用[附件存储](../../data/question-file.md)与[图像阅读工具](image-reading.md)。
5. Core 权威提交后发布公开输出；最终任务、摘要、工作区、原生生成及记忆待处理记录沿用[节点落盘机制](../../data/node-persistence.md)。没有自动调用 embedding。

最多 10 份 PDF，每份 10 MiB，总量 20 MiB。multipart 限制 10 个文件、5 个普通字段，拒绝额外/重复字段。激活后使用 PyMuPDF 验收，不在注册响应前进行页面渲染。

`QuestionFileService` 对一批文件部分保存失败进行补偿，保存线程未结束时取消须先等待写入结果；节点登记前失败，仅在数据库明确确认 UUID 尚未登记时删除新文件。数据库响应不确定则保留文件并脱敏记录，不能误删可能已提交的附件。断电等异常仍可能留下未登记文件，不自动扫描真实目录。

---

## SSE 与恢复

事件使用 `id: <sequence>`、`event: <type>`、JSON `data`。序号在单任务内递增；心跳为 `: heartbeat`，不占序号。支持 `turn.status`、`task.progress`、`message.delta`、`message.completed`、`error`。

Core 目前按通过校验且已落库的完整公开输出调用发布器，因此一条输出发送一次包含完整正文的 `message.delta`，随后 `message.completed`。这不是未经校验的模型逐 token 直播。前端按 `message_id` 累积 delta，收到 completed 后以其 `text` 覆盖正文；消息 ID 与打开树返回的 SQL 消息 ID 相同。原生推理、工具参数、私有生成、工作区内部内容和图片 base64 不进入 SSE。

- 首次激活期限 180 秒，超过后为 `expired`，释放暂存文件字节，订阅返回 `410`。
- 断线只释放订阅；生产者继续。多次或并发订阅不重复执行模型。
- 事件缓存每任务最多 512 条，任务快照保存累计公开消息；终态保留 1800 秒。
- `Last-Event-ID` 缺省 0，必须是最多 20 位 ASCII 非负整数。超前或过旧返回 `409`；先读快照、以 `last_sequence` 重连。流已开始时用无 `id` 的连接级 `error` 提示恢复，不更改业务终态。
- 空闲每 15 秒心跳。进程最多驻留 128 个任务，暂存附件总量最多 256 MiB；满时 `503`，不驱逐正在执行的任务。每秒清理过期任务。
- 模型设置和客户端首次激活时装配，未设置模型地址不影响只读树 API 启动。总执行时限沿用 Core；不读取或修改其他项目配置。

这些容量由服务构造参数明确提供，当前没有新增 `.env` 配置项。

展示过程沿用 contract-service-be 的精简事件投影：独立于有界回放队列保存 `turn.status/task.progress/message.completed/error`，过滤 delta、心跳和连接级错误，但保留原始事件序号及最后游标。字段定义见[打开问答树](../../../api/exploration.md#打开问答树)。任务快照另返回最近动作 `progress`，终态保留仅供展示。

本项目使用 MySQL 的 `question_node_contents.display_history` JSON 列，部署需手工执行 [010 迁移](../../../../schema/010-question-node-display.sql)。该列与上下文 `content` 独立，节点检查点不会覆盖它，也不会把动作记录注入模型或生成向量；整树删除随正文行一起清理。保存时校验用户、树和节点归属，并持树行锁拒绝游标回退、历史改写及已记录终态后的追加；展示写入不推进模型 revision。

有 SQL 节点后，事件快照先保存成功再进入 SSE 队列；节点登记之前的 pending/processing 暂存在内存，节点就绪时一起保存。没有创建 SQL 节点便终止的待激活或准备失败任务仍只驻留内存，不制造空节点。写入等待最长 5 秒，取消须等待本次提交或回滚完成；数据库故障停止任务，实时发送脱敏失败通知，已保存前缀与 SQL 消息保留。失败通知本身可能无法保存，打开树以节点当前状态为准，不伪造缺失事件。登记、节点终态和展示快照分属事务，崩溃也可能留下末尾不完整的展示记录；不宣称与 SQL 状态跨事务原子提交。

完整展示历史不受 512 条回放窗口限制。当前按任务写入完整 JSON 快照，适合现有受轮数限制的任务；读树时在一致性事务中批量加载，未引入独立事件分页接口或后台补写器。


---

## 停止、删除与生命周期

本项目没有合同业务门禁，待激活和执行中都可以显式取消。取消等待生产协程退出，已创建节点落为 `cancelled`，公开 `context_status=user_manually_stopped`。重复取消已取消任务幂等，其他终态返回 `409`。取消与最终提交交错时回查 SQL，已完成结果不改为取消；数据库状态不能确认时不得假称取消成功。

`ExplorationTreeService.operation` 为同树注册、激活、打开及删除提供同一进程锁；模型生成不长时间持锁。删除先取消并等待该树执行者、移除待激活及事件状态，然后执行已有 SQL 全量删除和附件清理。这样本进程后台生成不会在删除完成后继续落盘。停机先停止执行、关闭图像 LRU，再关闭仓库；系统停机的已登记任务按 `failed` 处理。

这是进程内任务调度，不是多实例共享任务队列：部署需要同一树及其任务请求粘到同一实例。SQL revision 仍拒绝外部并发写入，但不能代替跨进程删除/文件写入协调。进程崩溃后 pending 输入与实时 SSE 队列不恢复；已保存的展示事件及历史游标可通过打开树取得，但不用于跨进程重放连接，遗留 generating 节点不会自动重放。正式多实例或崩溃恢复调度需另行设计。已通过注册接口接入 `supersedes_turn_id`，旧任务的替代关系和已登记接替节点持久化；SSE 与 pending 输入仍遵循上述进程边界。


---

## 改变方向与单纯停止

沿用参考项目的语义：`POST .../turns` 携带 `supersedes_turn_id`，在注册时替代旧任务，再由新任务的首次 SSE 订阅激活。`cancel` 只停止，不创建新任务。

| 操作 | 旧任务状态 | 上下文语义 | 关联 |
| --- | --- | --- | --- |
| 单纯停止 | cancelled | user_manually_stopped | 不产生接替关系 |
| 改变方向 | superseded | user_goal_adjusted | 旧 superseded_by_turn_id → 新；新 supersedes_turn_id → 旧 |

方向调整继承旧任务的推进/探索模式，selected_node_id 省略或等于旧任务 ID，不能同时选择另一条路径。已结束任务不能再改写；处理中的任务须先有已登记 SQL 节点。pending 且没有 SQL 节点时，可在内存直接替换输入，新任务沿用原选择起点，不伪造尚未执行的节点历史。

已落库任务使用 `MySQLNodeRepository.replace_task` 的原子事务：保留旧内容和工作区，创建新的 generating 子节点，再将旧节点置为 superseded 并指向新节点。这个 generating 新节点只作待激活占位；模型未执行，HTTP 状态仍是 pending_activation。新 PDF 在事务前验收和准备，事务失败按数据库实际登记情况补偿文件。事务回执丢失时只读核对稳定新旧 ID 和输入，不盲目重放。

替代期间持同一树操作锁；短时事件条件锁跨越 SQL 替代及内存投影，防止旧生产者先发送错误的 failed/cancelled。确认关系后才取消并等待旧生产者，迟到模型响应只允许保存私有审计。HTTP 请求取消也要等替代事务和内存投影收尾；停机等待在途替代后再清理新 pending 节点。若提交和后续核对均不可用，则返回 503，保留可能已登记的文件；数据库故障期间不能承诺跨 SQL/内存的结果已确定。

新任务激活调用 `QuestionAgentService.generate(resume_registered=True)`，只接受尚未执行、输入完全一致的已登记接替节点，不再次创建或替代。上下文包含旧任务已接受的交互、用户调整方向的结束语义及旧节点快照。方向调整不通过取消来伪造替代，也不会自动恢复旧任务。

占位接替节点显式取消需先确认 SQL cancelled；过期与停机按 SQL failed 收尾，HTTP 分别为 expired 或服务关闭。过期清理失败保留待清理状态并重试，不释放成可并发写入的空槽；删除树继续使用原有停止与全量清理编排。

---

## 隔离验证

`tests/test_exploration_turn.py` 覆盖认证、严格输入、模式矩阵、待激活、并发订阅、回放、心跳、断线、期限、容量、取消与删除协调。`tests/test_exploration_turn_mysql.py` 在显式临时 MySQL 端口下验证实际 Core、PDF 图像阅读、消息 ID 一致性、分支落盘、原生生成保存、失败文件补偿及停机清理。模型使用确定性替身，文件仅写 `tests/tmp`；不以这些结果宣称已完成真实模型/共享数据库前端联调。

改变方向专项测试为 `tests/test_exploration_supersede.py`、`tests/test_exploration_supersede_mysql.py`，覆盖新旧事件关联、输入失败保留旧任务、SQL 回滚、丢失提交回执核对、并发替代、最终提交竞态、接替节点的取消/过期/停机及请求取消。


展示恢复专项为 `tests/test_display_history.py`、`tests/test_display_history_mysql.py`：覆盖旧记录、不截断历史、取消等待提交、数据库故障、真实 MySQL 重建服务读取、用户隔离、历史幂等/防回退及删除。网页 SSE 测试逐项比较实际事件与打开树展示记录；改变方向测试检查旧/新任务历史关系。
