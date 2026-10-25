# 节点生命周期与用户控制

## 职责与参考边界

参考 contract-service-be 的 Communication 状态管理，区分用户手动停止、用户调整方向、技术失败和正常交付，并分别约束上下文、落盘与生产协程。树父链、独立摘要、节点快照与 MySQL 事务沿用本项目实现，不迁入合同门禁、SQLite 或线性会话存储。

当前提供内部 Python 控制接口，保留取消、失败及原子 supersede；尚未接入问答 HTTP/SSE。rejected 属于业务准入，不在当前通用探索任务中虚构对应路径。

---

## 节点状态与派生信息

保留本项目已有命名，以免混淆节点与单次模型生成状态：

| 节点状态 | 对应源项目语义 | 行为 |
| --- | --- | --- |
| `generating` | processing | 执行中，可检查点提交、停止或替代。 |
| `complete` | completed | 本轮已正常交付唯一完整 final；澄清和限制说明也是正常交付。 |
| `cancelled` | cancelled | 用户手动终止。 |
| `superseded` | superseded | 旧执行被用户新要求替代。 |
| `failed` | failed | 模型、工具、超时、存储等技术失败；停机封闭活动任务也使用此状态。 |

摘要节点始终为 complete，终态不能重新激活或被迟到的业务写入覆盖。已结束后用户继续提问，创建普通新节点，不能把原节点重写成 cancelled / superseded。单次原生生成仍使用 complete / failed / cancelled，与所属任务的终态独立。

[QuestionNode](../../../../app/model/question_tree.py)提供只读属性：

- `context_status`：cancelled 派生 user_manually_stopped；superseded 派生 user_goal_adjusted；其他状态为空。
- `can_interrupt`：仅活动任务为 true，不是客户端可写权限。
- `processing_duration_ms`：有 finished_at 时按创建至结束时间计算，终态重放不改变；旧记录缺少结束时间时为空。

这些属性不重复存入 SQL，也不由调用方声明；当前属性不会随基础模型 model_dump 自动导出，后续事件/响应 DTO 应显式读取，不维护另一套可漂移状态。

---

## 持久化与原子替代

手工执行 [008-question-node-lifecycle.sql](../../../../schema/008-question-node-lifecycle.sql)，为节点增加 superseded、finished_at、superseded_by_node_id，为消息增加 completed / interrupted。新旧节点关系使用同树外键及唯一约束，构树时进一步校验接替者必须是旧节点的任务子节点。旧结束时间保留 NULL，不伪造历史执行时长。应用不自动执行 DDL，真实业务库未迁移。

[MySQLNodeRepository](../../../../app/repository/node_persistence.py)提供：

| 方法 | 约束 |
| --- | --- |
| `cancel_task(..., events=())` | 持树行锁按最新版本取消；精确重复停止幂等，不能改写其他终态；可同批保存 interrupted 正文。 |
| `replace_task(..., replacement_id, question)` | 原子冻结旧任务、登记新任务/问题/记忆/快照、建立接替关系并推进版本。 |
| `fail_task(...)` | 系统控制故障封闭活动任务，不能改写已完成或其他终态。 |
| `append_terminal_generation(...)` | 终态仅追加私有原生生成，稳定 ID 幂等；不接受公开消息、工具提交或工作区变更。 |

替代关系为“旧任务 → 接替任务”，新任务继承旧任务最后成功提交的快照。旧任务在主干时新任务推进主干；旧任务在分支时新任务仍属该分支。登记失败时旧任务保持活动且新节点不存在；只有登记成功才通知本实例旧生产协程取消。精确相同 ID 与新问题的重复替代返回已登记节点，其他替代请求拒绝。

事务内部先将旧节点临时封闭以复用父链校验，再插入新节点、设置真实 superseded 关系；其他事务不能看到中间状态。用户停止、替代和普通检查点/完成共享同一数据库树行锁，先提交者决定终态，不用进程锁宣称跨实例停止远端协程。

---

## 内部服务与生产协程

[QuestionAgentService](../../../../app/service/question_agent.py)按可信用户、树、节点登记本实例生产协程，同一节点不允许两个生成请求并行。公开内部接口为：

```python
# service 为 exploration.build_service(...) 或现有 QuestionAgentService。
# old_node_id 必须已经完成节点登记，用户有权访问且仍在生成。
await service.cancel(user=user, tree_id=tree_id, node_id=old_node_id)

# 用户调整方向：原子登记新问题并替代旧任务，随后执行新任务。
result = await service.generate(
    user=user, tree_id=tree_id, node_id=new_node_id,
    supersedes_node_id=old_node_id, question="只分析新的目标范围。",
)

# 宿主退出时先封闭活动任务并等待生产协程，再关闭仓库和模型客户端。
await service.close()
```

替代入口不能同时传 selected_node_id；起点由服务端绑定旧节点。问题、思考强度等输入先校验，不能发现新输入非法时旧执行已经被停止。原问题、已接受交互、快照及私有生成不会被删除。替代节点已在控制事务中登记，后续由同一 Core 执行；容量整理发生在新任务内部，不为该已登记任务再插入前置摘要或重接父链。普通历史选点的任务前独立摘要流程保持不变。

生成服务还提供 `supersede(...)` 作为低层控制适配，返回已登记的新任务；常规宿主应使用 generate(supersedes_node_id=...) 完成执行，不能只登记后遗漏生产调度。重复生成正在执行的接替节点会冲突，但不能因此把合法生产协程标成失败。

停止/替代在数据库确认后取消本实例协程，并按 cleanup_timeout_seconds 有界等待；远端模型是否停止计算依赖远端能力。其他实例的协程无法通过本地 task.cancel 结束，但迟到提交仍被数据库终态阻断。若生产者不响应取消，本地有界等待后保留其引用直到退出，不声称远端已经停止。

close 将已登记的活动任务置 failed，再取消并等待；不可用的数据库清理只脱敏记录异常类型。还未创建节点的准备阶段不能伪造已落盘状态。主应用已通过问答运行时装配 close，先保存中断预览并封闭任务，再关闭仓库。

---

## 消息、私有生成与上下文

消息 completed 表示完整已验收输出，interrupted 表示展示中断正文；后者可以是 intermediate 或 final，但不能成为 TaskContent.answer、不能满足任务 complete 的 final 要求，也不登记向量记忆。用户问题必须为 completed。streaming 是未来传输期间的临时状态，delta 不直接落入当前消息表。

终态拒绝公开输出、工具动作和工作区的迟到提交。NodeJournal 发现外部终态时只保存迟到的原生响应/推理/流片段，保持任务终态，再阻止该响应触发工具动作。纯私有追加不改变树公开 revision，避免旧请求审计让新任务无故冲突；记录序号仍由树行锁串行分配，完整审计仍可恢复。

用户显式提交新问题时，可以选择 complete / cancelled / superseded / failed 节点，仍按父链向上找最近摘要、恢复该节点已提交快照。不会自动重新执行原节点，也不会从 generating 节点继续：

| 历史状态 | 模型公开历史投影 |
| --- | --- |
| complete | 原问题、完整已接受中途输出和最终答复。 |
| cancelled | 原问题及“用户已终止，不继续执行此请求”，不注入该轮中途或半段最终输出。 |
| superseded | 原问题、完整已接受中途输出及“用户已调整方向，按最新要求处理”，不伪造最终答案。 |
| failed | 原问题、完整已接受中途输出及失败标记，不把已保存过程说成成功结果。 |

interrupted 消息和私有推理不进入上述历史；结束标记只是历史事实，不是新命令。任务前摘要与主循环整理都使用同一投影，避免摘要重新带回停止轮次的半成品。用户停止后已保存的中途消息仍可用于历史展示/审计，但从记忆检索和来源回查中排除；原问题仍保留。[自动向量化](../../../capability/application/memory-embedding.md)遵循相同的停止边界，只加工终态任务的有效完整消息。

---

## 验证

[状态专项测试](../../../../tests/test_node_lifecycle.py)覆盖派生标记、时间、关联、消息状态、历史投影、取消与替代幂等、登记回滚、迟到私有审计、停止/完成与停止/替代竞态、真正取消生产者、远端忽略取消、重复接替请求、停机清理、显式失败后续问及新输入校验。所有模型均为确定性替身，MySQL 使用独立一次性容器，不读取真实配置，不请求共享服务。

2026-10-21 验证：新增 5 项离线与 13 项隔离 MySQL 测试通过，包含准备阶段、外层生成超时正确归为 failed 的检查。完整回归 385 项通过，无跳过，其中 82 项为独立 MySQL/MyVector 验证；`pip check` 无冲突，8 份维护文档的 103 个相对文件链接有效。临时容器与卷已清理，真实 `.env`、业务数据及其他项目未修改，未执行真实模型或共享中间件联调。
