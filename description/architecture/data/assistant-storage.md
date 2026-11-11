# 个人助理、任务与提醒存储

> **当前部署入口：** 新空库仅执行 deploy 的[最新初始化脚本](../../../../knowledge-accumulation-deploy/mysql/init/001-schema.sql)。本文中的编号迁移和旧库操作属于历史演进记录，不是新部署步骤；历史 SQL 已从当前仓库移除，仅可从 Git 历史追溯。已有数据库须另行制定升级方案，不可重跑初始化。

## 用途与实现范围

为“一位用户、一个助理、一棵 assistant 树”准备数据库约束、已领取任务记录、可靠交付记录及定时/周期提醒的数据契约。助理按任务串行执行，复用既有[问答树](question-tree.md)、[节点落盘](node-persistence.md)、工作区、记忆与 [Core 附件查看工具](../workflow/question-answering/image-reading.md)。

存储基础设施提供 [011-assistant-storage.sql](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/011-assistant-storage.sql) 和 [assistant.py](../../../app/model/assistant.py) 中的持久化模型。不自动建表，不创建真实助理或提醒。另已提供[助理局部上下文读取](../workflow/question-answering/assistant-memory.md)和[中间件队列传输及单次消费编排](../../capability/infrastructure/assistant-queue.md)；绑定、执行账本、交付仓库及[后台消费与发送器](../../capability/application/assistant-consumption.md)已经实现，默认关闭；[提醒查看、创建、删除和调度器](../../capability/application/assistant-reminders.md)已接入，未开始消费真实中间件任务。

另已接入[私有画像存储与增删事务](../workflow/question-answering/assistant-personal-context.md#同事务提交与恢复)：016 保存逐条偏好，017 提供私有数字序号与动作去重账本。助理启动会校验相关表；增删与节点检查点同事务提交，旧动作重放不会重复新增或复活已删除条目。需在启用消费者前完成迁移，应用不自动执行 DDL。

中间件负责 `/middleware-service/api/assistant/tasks/` 下的发布、领取、租约、确认、优先级和短消息合并；本地不复制第二份待领取队列。提醒、用户消息、其他助理消息、心跳均进入同一棵树，由宿主从主干末端继续。助理模式不向用户提供选择节点或打断当前任务的入口。任务及用户片段保存 `source_assistant_id`，与接收方 userid、认证平台代码分开；assistant_message 必须具备来源助理。该字段保存在现有 input_payload JSON，不新增 SQL 列或迁移。来源校验和 finish_task 的显式通知选择规则统一见[队列契约](../../capability/infrastructure/assistant-queue.md#任务结束与消息发送)。

---

## 表关系与约束

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `assistants` | 用户与唯一助理树、启停及心跳设置 | `userid` 主键、`tree_id` 唯一；复合外键确保树属于该用户且用途为 assistant。 |
| `assistant_robot_inbox` | Stream 接收去重、整条校验后的待发布资料及拒绝通知 | 014 新增；按消息 ID 唯一，详见[入站账本](../../capability/application/assistant-inbound.md#接收账本与恢复)。 |
| `assistant_task_executions` | 已领取任务的冻结输入、附件映射及处理/确认状态 | 中间件 `task_id` 主键、任务节点唯一；012 后每位用户至多一个 received/prepared/running 任务，完成但未确认的任务可以保留。 |
| `assistant_reminders` | 提醒内容、规则及下一次计划时间 | 提醒归属助理；来源任务若存在必须属于同一用户。 |
| `assistant_deliveries` | 待发送正文、稳定去重键、重试与回执 | 任务动作或提醒发生时刻二选一；来源必须属于同一用户。 |

所有用户标识区分大小写，不外键关联 `employees`。助理任务正文、最终回复、原生生成、工作区和记忆继续保存在既有节点表，不在执行记录中复制一份模型历史。文件归属以任务节点内的附件登记为准。

迁移同时在 `question_trees` 增加仅对 `kind='assistant'` 生效的用户唯一索引，探索树数量不受影响。采用函数索引，不增加 `SELECT *` 可见字段，兼容既有严格树模型。MySQL 支持函数键的 UNIQUE 约束，见[官方 CREATE INDEX 说明](https://docs.oracle.com/cd/E17952_01/mysql-8.4-en/create-index.html)。

这些约束保证每位用户**至多一棵**助理树；创建助理时还须在同一事务创建/绑定树，数据库不会主动为每个用户生成助理。绑定后的树不可被直接删除，生命周期清理需另行编排。现有探索删除入口仍只接受 exploration。

---

## 助理设置

| 字段 | 说明 |
| --- | --- |
| `userid` / `tree_id` | 所属用户及唯一助理树。 |
| `tree_kind` | 固定 `assistant`，用于复合外键，不能借此绑定探索树。 |
| `enabled` | 是否允许调度，默认 true；另受全局 ASSISTANT_ENABLED 控制。 |
| `created_at` / `updated_at` | UTC 创建及更新时间。 |

已接入[空闲心跳调度](../../capability/application/assistant-heartbeat.md)，发送前查询中间件 `GET /assistant/tasks/status`：有任务执行时不发送，以中央上次成功确认心跳时间判断间隔；同一确认周期使用稳定发布键避免重复排入等待中的心跳。不在本地保存另一份权威忙碌状态或上次心跳时间。

---

## 已领取任务与附件

| 字段 | 说明 |
| --- | --- |
| `task_id` | 中间件任务标识，重复领取复用同一记录。 |
| `userid` / `tree_id` / `node_id` | 用户、唯一助理树、实际任务节点；准备前可以没有节点。 |
| `task_type` | `reminder`、`user_message`、`assistant_message`、`heartbeat`。 |
| `input_payload` | `QueueTaskSnapshot`：冻结任务及合并前的 `user_messages`、来源、时间、文件引用。 |
| `file_bindings` | `TaskFileBinding[]`：中间件文件引用到本地 `AttachmentReference` 的持久映射。 |
| `processing_status` | `received`、`prepared`、`running`、`completed`、`failed`。 |
| `error_code` | 脱敏的稳定错误分类，不保存原始异常或凭据。 |
| `received_at` / `updated_at` / `finished_at` | 本地接收、更新、处理结束时间。 |
| `acknowledged_at` | 已成功确认中间件任务的时间，与模型完成分开。 |

`received` 可以保存部分文件映射，供失败后恢复；进入 `prepared`、`running`、`completed` 时，所有来源附件必须有唯一完整映射。`running` 和 `completed` 必须有节点，`completed/failed` 必须有结束时间，非终态不得记录 ack。

011 的未确认任务唯一约束在 012 中改为活动执行唯一约束，已完成但等待重领确认的记录不阻塞新的高优先级任务。失败任务自动确认消费并跳过，保留 failed、错误码及全部已有轨迹，不重放模型或发送动作；ack仅代表消费终结，不表示业务成功。失败任务的未交付记录不阻塞队列，继续保留供核对。`finish_task`、交付成功和中间件 ack 是三个不同阶段，已经完成的模型回复不应因发送/ack 重试而重新生成。

任务 ID 唯一约束本身不是执行锁。现有消费者另通过 MySQL 独立连接的命名锁串行同用户，并续租中央任务；交付使用 attempt_count 原子抢占，完整边界见[消费与交付](../../capability/application/assistant-consumption.md)。不宣称外部机器人通知恰好一次。

### 合并消息

`QueueTaskSnapshot` 保留原始 `user_messages` 顺序，不按时间重新排序。用户消息必须有原始片段，片段 ID 唯一；每条消息保留自己的来源、时间和附件。根文件列表必须等于片段文件顺序汇总，根来源和请求 ID 取首条，根时间取最晚片段。非用户任务不允许伪造合并用户消息。

`input_payload` 不保存平台 Bearer、用户 Bearer 或 `delivery_token`。工程任务标识及恢复状态也不能直接作为提示词注入；后续宿主应将业务消息投影到既有上下文渲染契约。

### 附件落盘及记忆查看

消费者先通过中间件附件接口取回文件，存入 `data/user/upload-file/`，并在任务中完成登记，再确认消息；中间件 ack 会清理来源附件，因此不能只保留下载地址。

继续使用 [UploadFileStore / QuestionFileService](question-file.md)，不新增附件表、另一个文件目录或第二套打开工具。来源 `file_id + file_extension` 与本地 UUID 分别保存到 `TaskFileBinding.source`、`TaskFileBinding.attachment`，重试复用映射。`AssistantTaskExecution.attachments` 提供节点附件，`message_attachments(message_id)` 恢复原始短消息的附件对应关系。

模型仍只看到“类型 + 本地 UUID”。记忆检索命中任务后，既有记忆页面渲染该任务附件引用，由 Core 的 `view_image_file` 按本树登记打开并复用 LRU。记忆索引继续处理用户问题、模型中途输出及最终输出，不在本阶段额外解析二进制附件、分块或向量化全文。助理工厂已支持注入 `memory_backend`；队列附件转存与消费者已经装配，默认关闭。

当前多图使用 PDF，音频/视频预留 MP3/MP4 存储契约，但 Core 暂不支持音频/视频查看。中间件可接收的文件格式不等于本平台已经实现相应转换器，后续接收边界必须明确校验，不得将任意字节改后缀后声称可以查看。

---

## 提醒规则

| 字段 | 说明 |
| --- | --- |
| `id` / `userid` | 本地提醒 ID、所属用户。 |
| `content` | 发布给助理的提醒正文，最多 10000 字符。 |
| `schedule_type` / `schedule_config` | `once`、`interval` 或 `calendar` 及对应严格结构。 |
| `timezone` | IANA 时区，默认 `Asia/Shanghai`，运行环境须提供时区数据。 |
| `next_fire_at` / `end_at` | UTC 下一次计划时间及可选截止时间。 |
| `status` | `enabled`、`paused`、`completed`、`cancelled`。 |
| `created_by_task_id` | 可选来源任务，必须属于同一用户。 |
| `created_at` / `updated_at` | UTC 审计时间。 |

Python 模型要求时间包含时区并转换为 UTC，SQL `DATETIME(6)` 存 UTC。启用时必须有下一次时间，结束/取消时必须清空；下一次不能超过截止时间。模型验证规则结构、时区和候选触发时刻，SQL 验证关系与基本状态；复杂 JSON 规则不能绕过模型直接写入。

以下仅展示 `schedule_config`：

```json
{"at":"2026-10-25T09:00:00+08:00"}
```

一次性 `once` 的下一次时刻必须等于 `at`。

```json
{"start_at":"2026-10-25T09:00:00+08:00","every_seconds":7200}
```

固定间隔 `interval` 从起始时刻按秒推进，不等于按本地日历“每天”；下一次必须落在非负整数周期上。

```json
{"frequency":"weekly","start_date":"2026-10-25","local_time":"09:00:00","weekdays":[1,3,5]}
```

日历 `calendar` 支持 `daily/weekly/monthly`。周一为 1，周日为 7；每天不填写星期/月内日期；每周必须给出不重复星期；每月必须给出 `day_of_month` 和 `missing_day`，例如：

```json
{"frequency":"monthly","start_date":"2026-10-25","local_time":"09:00:00","day_of_month":31,"missing_day":"last_day"}
```

缺失日期的 `skip` 表示跳过该月，`last_day` 表示取月末，不隐式决定。到期扫描索引为 `(status,next_fire_at,id)`。

调度在同一事务插入本次交付并推进 `next_fire_at`，然后执行外部发布。一次性提醒写入可靠交付记录后完成排期（`completed`、下一次时间为空）；这不表示消息已经发出，发送成功仍以交付记录为准。漏掉多次周期合并一次补发；时间计算、夏令时规则、三个工具与发布重试的权威说明见[助理定时任务](../../capability/application/assistant-reminders.md)。删除设为 cancelled 并清空 next_fire_at，保留已发生交付；不撤回已生成的交付或中间件任务。本阶段不提供暂停和恢复工具。

---

## 可靠交付记录

| 字段 | 说明 |
| --- | --- |
| `id` / `userid` | 持久交付 ID、所属助理。 |
| `task_id` / `reminder_id` | 关联任务动作或到期提醒，必须且只能选择一种来源。 |
| `action_key` | 任务内稳定动作标识；最终回复及多条助理消息用不同键。 |
| `scheduled_for` | 仅提醒必填，标识本次计划发生时间，不使用实际重试时间。 |
| `destination` / `recipient_userid` | `user` 或 `assistant`，以及接收用户/助理所属用户。 |
| `task_type` | 用户回复为 null；助理间通信为 `assistant_message`；到期提醒为 `reminder`。 |
| `message` | 纯文字，不支持助理间文件转发。 |
| `status` | `pending`、`sent`、`failed`，与模型处理状态独立。 |
| `attempt_count` / `next_attempt_at` | 重试次数与可选下次发送时间。 |
| `sent_at` / `external_receipt` / `error_code` | 成功时间、脱敏业务回执、脱敏错误分类。 |
| `created_at` / `updated_at` | UTC 审计时间。 |

唯一 `(task_id,action_key)` 防止恢复时重复产生同一发送动作，唯一 `(reminder_id,scheduled_for)` 防止重复产生同次提醒。`AssistantDelivery.request_id` 固定为 `assistant-delivery:<id>`，发送重试不得重新生成 ID；外部是否支持幂等仍由实际发送契约决定，本地唯一记录本身不保证网络发送恰好一次。

提醒只能投递给所属用户的助理；任务内对其他助理发送文字使用 `assistant_message`。来源关系受同用户复合外键约束，发送工具校验目标 SQL 用户和助理启用状态，并从当前任务归属绑定发送者，不能信任模型自报身份。用户回复由 finish_task.send_to_user 明确选择，保存后走钉钉 OpenAPI，不新增中间件结果接口。

---

## 迁移与验证

2026-10-26 已在本地容器 `phenosolar-knowledge-dev-mysql-1` 的 `registry` 库补齐 [015 员工任职资料](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/015-employee-work-profile.sql)、[016 画像表](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/016-assistant-portraits.sql)和 [017 画像动作](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/017-assistant-portrait-actions.sql)，`dev_schema_migrations` 已记录全部 17 份迁移及文件 SHA256。原有员工字段与原有业务表记录数保持不变，历史员工新增任职字段采用未知值，未调用钉钉补采。

本地验证通过：`MySQLAssistantRepository.validate_schema()`、`RobotInboxRepository.validate_schema()`、画像索引、任职资料约束、姓名 ngram 查询及全部员工任职 JSON 校验；画像序列已初始化为 `id=1,next_key=1`。该状态仅对应上述本地数据库，不代表其他部署环境已迁移。助理运行开关仍关闭，未重启服务，也未执行真实钉钉消息收发。

先完成既有问答业务迁移，再手动执行 011；启用消费者还需执行 [012 消费迁移](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/012-assistant-consumption.sql)，增加独立的 ack 凭证/租约列并调整活动执行唯一约束。不自动修改真实配置或数据。011 自身不依赖 MyVector 函数，检索插件按既有[部署说明](../../capability/infrastructure/myvector.md)独立准备。

上线前先检查历史重复助理树：

```sql
SELECT owner_userid, COUNT(*) AS tree_count
FROM question_trees WHERE kind='assistant'
GROUP BY owner_userid HAVING COUNT(*) > 1;
```

重复时唯一索引创建失败，不能自动删树或挑一棵覆盖。已有单棵助理树还需后续受控创建 `assistants` 绑定，本迁移不猜测启停/心跳配置。MySQL DDL 不能依赖整文件事务回滚；部署应逐项确认执行状态，不重复盲跑。

[契约测试](../../../tests/test_assistant_storage.py)覆盖严格输入、合并消息、附件映射、执行状态、时区/周期和纯文字交付。[独立 MySQL 测试](../../../tests/test_assistant_storage_mysql.py)验证真实 DDL、一人一树、跨用户关联拒绝、并发重复任务、未确认任务互斥、动作/提醒去重、事务回滚，以及附件落盘后重新构造读取服务、记忆回读及 Core 图像查看。

实库测试只接受显式 `ASSISTANT_STORAGE_TEST_MYSQL_PORT` 的独立临时容器，固定使用测试库 `knowledge_assistant_test`，禁止默认 3306；会重建测试表，不得指向业务库。所有附件样本在 `tests/tmp/` 并自动清理，不读取实际 `.env`，不调用共享中间件或真实模型。

2026-10-24 验证：新增 10 项数据契约及 10 项独立 MySQL 8.4 测试全部通过。完整 unittest 共 1105 项，943 项通过、162 项因其他隔离环境未配置而跳过。本次助理实库测试全部实际执行，未验证真实中间件消费、提醒调度或模型生成。


---

## 当前工作区与驻留窗口

个人助理的最新工作区改为 `assistant_workspaces` 单行存储，节点不再绑定历史工作区版本。该行同时保存最新压缩摘要和未覆盖任务的读取下界，事务提交后更新跨任务驻留窗口。新版本启用前须执行 021 DDL 和显式迁移器，清理已有 assistant 历史工作区；exploration 不参与此迁移。完整契约与验证见[助理记忆驻留](../workflow/question-answering/assistant-memory.md)。


---

## 用户附件交付记录

027 迁移在 `assistant_deliveries` 增加可空 `file_id`。NULL 保持原正文/队列消息语义；非空仅允许任务向所属用户本人交付用户附件，使用 `file:<UUID>` 动作键，与正文 `final` 分别去重。没有对上传表增加外键：交付账本需要保留文件被删除后的历史，发送前由业务服务重新检查附件可用性。事务登记、类型限制与恢复规则统一见[最终回复附件交付](../../capability/application/assistant-consumption.md#最终回复的用户附件交付)。
