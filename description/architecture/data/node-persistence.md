# 节点落盘与恢复

## 用途与边界

在[问答树](question-tree.md)结构之上保存用户提问、完整阶段输出、最终答案、每次模型生成和工作区快照。实现入口为 [MySQLNodeRepository](../../../app/repository/node_persistence.py)，数据模型为 [NodeMessage / NodeGeneration](../../../app/model/node_persistence.py)。已装配 Agent Core 和内部树生成服务，不自动恢复或重放工具动作。

采用显式事务检查点，成功返回才表示已落盘。进程在两次检查点间退出时，尚未提交的流式片段不能保证恢复；没有后台写入队列或自动重放机制。

---

## 存储结构

手动执行 [004-node-persistence.sql](../../../schema/004-node-persistence.sql)，依赖既有 `001` 问答树和 `002` 记忆迁移；当前模型还要求 `006`、`007` 类型字段和 `008` 生命周期字段，迁移及历史分支回填前提见[问答树结构](question-tree.md#mysql-数据结构)。`003` MyVector 缓存按需部署。助理还依赖[当前工作区与压缩边界迁移](../workflow/question-answering/assistant-memory.md)。应用不自动迁移真实数据库。

| 表 | 内容 |
| --- | --- |
| `question_node_messages` | 不可变消息 ID、所属节点、question/intermediate/final、completed/interrupted、正文、节点内事件序号、时间。 |
| `question_node_generations` | 不可变生成 ID、所属节点、complete/failed/cancelled、完整 JSON payload、事件序号、时间。 |

两表共用节点内连续事件序号，树行锁保证分配串行，恢复时检查跨表重复及缺口。这两张事件表之外，当前还有工作区、助理、提醒、画像等业务表；各模块迁移独立维护，MyVector 为可重建缓存。

生成 payload 保留完整 `assistant_message`、`raw_response`、`raw_chunks`、`usage`、`finish_reason` 和脱敏 `error_type`，包含服务返回的原生推理，不使用轮数窗口或长度截断。失败、纠错及取消生成也可以独立提交。`complete` 仅表示一次完整模型响应，不代表工具动作已被业务接受。

公开消息与私有生成分开：只有调用方通过全部校验的交互才写入 `TaskContent.exchanges`；仓库不会从原始生成自动构造权威交互或工作区。私有推理不自动注入后续模型，也不自动登记为记忆。

---

## 接口与调用顺序

仓库复用 `MySQLQuestionTreeRepository` 的连接池和事务，统一关闭原仓库即可；每次调用必须传入上游已认证的 `owner_userid`。

宿主可注入 `commit_hook(cursor, tree, node_id, content, status)`，在节点检查点或终态写入后、同一事务提交前执行附加 SQL。回调失败与节点一起回滚；回调不得进行网络交付。这是 SQL 事务扩展点，与 Core 注册项的[工具执行后钩子](../workflow/question-answering/agent-core.md#工具执行后钩子)不同：后者在工具执行后、进入节点提交前处理结果，每个工具独立注册。当前用于[助理交付账本](../../capability/application/assistant-consumption.md#处理与一致性)，将已接受发送动作与节点原子保存，提交后才调用机器人或中间件。

| 方法 | 使用方式 |
| --- | --- |
| `start_task` | 预先生成稳定 node_id，传 question、attachments、parent_id、expected_revision；原子保存 generating 节点、附件引用、提问、pending 记忆及初始快照引用。 |
| `checkpoint` | 按接收顺序提交 messages/generations，可同时提交新 workspace 和已接受 exchanges；树 revision 加一。 |
| `finish_node` | complete 必须携带 final 消息；failed/cancelled 不产生伪造最终答案；可原子提交最后一批记录及快照。 |
| `load_node` | 返回树、任务正文、准确快照、所有消息及所有生成，可通过 events 获取跨表事件顺序。 |
| `load_memory_source` | 由 memory_id 校验用户、节点及来源类型后读取不可变原文。 |
| `find_file` | 按用户和树归属，从任务正文查出 UUID 对应的附件类型；磁盘读取由[文件服务](question-file.md)负责。 |

exploration 根任务创建空 JSON 工作区版本；子任务继承所选父节点的实际版本。内容变化创建新版本，不修改旧快照，不从树的最新叶子取代历史父节点状态。Agent 入口会将空对象转为规范四区域工作区并校验其他快照，见[上下文与工作区](../workflow/question-answering/context-management.md)。

个人助理仅在 `assistant_workspaces` 保留一条最新工作区，节点不再绑定历史版本；压缩摘要和保留边界与工作区同事务更新，提交后同步驻留窗口。任务内压缩按实际覆盖前缀驱逐，任务前压缩也在登记新任务时保存整理后的工作区。迁移与读取语义见[助理记忆驻留](../workflow/question-answering/assistant-memory.md)。

探索 `start_task` 与底层 `append_path` 共用持锁追加路径，自动保存任务及可选摘要的 `path_type`；检查点、终态提交和稳定 ID 重放保留已有归属。`load_node` 返回包含树 `kind` 和节点 `path_type` 的恢复结果。助理 `load_agent_context` 优先复用压缩后的驻留窗口，冷启动按持久化覆盖下界读取，`start_task` 走局部线性追加，不构建完整树；两个入口均拒绝选择旧节点或创建分支，详见[助理记忆加载](../workflow/question-answering/assistant-memory.md)。通用树仓库仍提供完整结构读取，不代表助理生成应调用这些入口。

每个公开消息与其 pending 记忆在同一事务登记，`source_id` 等于消息 ID；提问 ID 等于节点 ID。输出必须是完整消息，不把流式碎片登记为最终消息。[自动向量化](../../capability/application/memory-embedding.md)在节点终态后异步读取完整消息并加工，不延长节点提交事务。记忆检索子 Agent 的原生生成以 `memory_retrieval` 用途写入当前节点私有生成记录，文件检索子智能体的原生生成及执行反馈使用`file_retrieval`用途；公开候选来源不读取这些记录。

上述记忆登记仅适用于 completed 消息，interrupted 只用于展示和审计。取消、原子替代、结束时间与终态私有审计追加见[节点生命周期](../workflow/question-answering/node-lifecycle.md)；私有追加不能改变公开状态或工作区。

---

## 幂等与失败语义

稳定消息/生成 ID 的相同内容重试不重复保存，已成功提交的精确重试允许携带旧 revision；同 ID 内容不同拒绝覆盖。新写入必须使用当前 revision。已接受 exchanges 只能追加，不能改写已有前缀；节点终态冻结，不允许迟到结果改写。

同一事务内包含消息、记忆、生成、快照、节点正文及 revision 变更。数据库错误、超时和取消均回滚；底层存储调用方须保留稳定 ID，以便确认失败后重试；Agent 生成不因此自动重放。错误类型只保存类名，不保存异常正文、堆栈或密钥。

旧结构仓库的 `append_path` / `finish_task` 保留为底层结构接口，不提供完整运行日志保证。新任务运行应使用本仓库；旧节点缺少真实快照或规范化提问时明确拒绝恢复，不能伪造历史。旧 `register_memory` 自定义 source_id 不保证可解析，本仓库回查不猜测来源。

摘要仍为独立树节点。选择 D 的上下文只回溯到 D 之前最近摘要，不读取后续分支摘要；新分支恢复 D 的快照。显式新问题允许选择 failed/cancelled/superseded 节点，并应用状态相关的历史过滤；这不重新激活原节点，也不自动重启其执行。

---

## 验证

[模型测试](../../../tests/test_node_persistence.py)覆盖字段约束、长原生推理及失败片段。[隔离 MySQL 测试](../../../tests/test_node_persistence_mysql.py)覆盖原子写入、重新连接恢复、事件顺序、稳定 ID 幂等、历史分支快照、摘要边界、权限隔离、终态冻结、revision 冲突、注入故障回滚、超时与取消回滚。执行方式见[问答树验证](question-tree.md#验证方式)。

2026-10-21：完整 280 项通过，无跳过，包括 38 项独立临时 MySQL/MyVector 实库测试；`pip check` 无冲突。未修改真实业务库、实际 `.env` 或应用启动装配。

Agent Core 进一步通过 `load_agent_context` 一致性读取公开路径，并允许 `start_task(summary=..., summary_id=...)` 原子提交任务前摘要与新任务。私有 GenerationPayload 增加 purpose，用于区分主生成和整理，不进入模型提示或新增 SQL 列。另以 `external_expert` 标记[外部专家的完整 Responses 与调用审计](../workflow/question-answering/external-expert.md#会话隔离容量与释放)，包括未提交的失败/截断响应；公开树与 SSE 不返回这些私有记录。完整上下文规则见[问答上下文管理](../workflow/question-answering/context-management.md)。

树用途与路径类型适配阶段的验证见问答树专项结果：当时完整 351 项通过，包含新结构下的实际 MySQL 加载与落盘、确定性模型替身驱动的 Agent 运行；该数量为历史记录。当前生命周期控制的验证见[最新状态专项结果](../workflow/question-answering/node-lifecycle.md#验证)，真实业务库和运行配置未改动。

网页精炼子图使用 `purpose=web_page_refinement` 保存完整原生响应与每次校验结果，失败/取消记录同样保留；不新增 SQL 列，公开输出隔离和详情见[网页工具专题](../workflow/question-answering/web-search.md)。

助理最终正文转换使用 `purpose=assistant_message_rendering`，工具执行后钩子通过当前 journal 保存完整原生响应、推理和失败/取消记录；生成完成不代表格式验收通过。全部钩子通过后，转换后的 `finish_task.content` 与公开答案经原有提交路径一起保存，后续上下文和交付读取该正文；无需修改问答树结构、仓库接口或 SQL 列。详见[转换审计与恢复](../workflow/question-answering/assistant-message-rendering.md#私有审计与交付恢复)。


---

## 公开展示历史

[010 迁移](../../../schema/010-question-node-display.sql)为 `question_node_contents` 增加独立的 `display_history` JSON 列，保存 SSE 精简展示事件及游标；旧节点为 NULL。此列不属于 `TaskContent`，不参与模型上下文、私有生成序号、工作区快照或记忆索引。节点内容更新只写 `content`，不会覆盖展示历史；整树删除一并移除。写入规则和故障边界见[展示恢复](../workflow/question-answering/exploration-session.md#sse-与恢复)。

---

## 部分历史摘要检查点

任务 `exchanges` 内的 `context_summary` 保存 `text` 与正整数 `covered_turns`。覆盖数量针对当时继承的有效祖先任务前缀，不含本轮任务；多次检查点顺序应用，恢复时拒绝范围越界。原生工具投影先校验完整配对，再排除检查点，不向模型展示覆盖字段。全部旧交换及私有生成仍保留；不新增SQL列，不修改其他分支。完整范围与恢复规则见[上下文管理](../workflow/question-answering/context-management.md#预算与压缩)。
