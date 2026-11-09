# 助理定时任务

## 用途与边界

为当前个人助理提供查看、创建、删除三个定时任务工具。到期后发布中间件 reminder 消息，由助理按正常任务流程处理，再通过 finish_task.send_to_user 决定是否通知所属用户。调度器不直接调用钉钉，不打断正在执行的任务。

已接入[助理消费生命周期](assistant-consumption.md)，与消费者共用 ASSISTANT_ENABLED，默认关闭。提醒不能发给其他用户；不新增修改、暂停、恢复工具或 HTTP 接口。需要调整时，先删除再创建。空闲唤醒另由[心跳调度](assistant-heartbeat.md)负责；钉钉入站发布及真实数据迁移不属于本模块。


三个提醒工具各自注册末尾渲染钩子，覆盖列表三态与空页、创建排期、删除或已取消无需重复；回执只在提交成功后进入模型上下文。`create_reminder` 通过 `persist_result=True` 保存生成的提醒 ID 与首次触发时间，提醒仓库同事务读取独立结构化结果；旧 JSON 历史保持兼容，不从新正文提取字段。后续检查点重放不重置已推进或已取消提醒。

---

## 三个工具

工具位于 [reminder.py](../../../app/agent/assistant/tool/reminder.py)，属于 assistant 专用具体实现，不进入公共 Core 或 exploration。消费宿主按本轮可信 userid 绑定仓库；模型不能传入 userid、来源任务、下一次排期或发送凭据。三个工具都声明 thinking。模型可见说明按[助理工具的场景式描述约定](assistant-consumption.md#两个助理工具)组织，保留时间规则、通知选择和失败边界，事务与调度实现只在本文说明。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| list_reminders | offset 默认 0；limit 默认 10，范围 1—50 | 查询当前助理的 enabled/paused 提醒；返回可折叠正文及 next_offset，null 表示没有下一页。 |
| create_reminder | content、schedule_type、schedule_config；timezone 默认 Asia/Shanghai；end_at 可选 | 预验未来触发时间，随节点检查点提交创建记录，返回提醒 ID、规则和首次排期。 |
| delete_reminder | reminder_id | 验证本人归属，随节点检查点关闭未来排期，从当前列表移除；重复删除幂等。 |

列表按创建时间和 ID 排序，是本次查询快照，不锁定跨页结果。若查询期间发生变化，可以从 offset=0 重新查询。失败返回资料不可用，不能当作空列表。每任务只保留最后一次已提交的列表页面；新查询或任务结束后旧页面不可读取，沿用 Core 的页面折叠协议。

列表正文示意：

```text
# 当前定时任务

提醒 ID：0123456789abcdef0123456789abcdef

提醒内容：
检查本周待办，根据实际结果决定是否通知用户。

时间规则：每周一 09:00:00，从 2026-10-26 起
时区：Asia/Shanghai
下次触发：2026-10-26T09:00:00+08:00
截止时间：未设置
状态：启用
```

创建结果只有节点事务成功提交后才进入模型有效上下文；数据库失败不会向模型交付“已创建”的结果。content 是未来交给助理的任务内容，不是提前发送的用户通知。模糊时间、月末规则或不明确的取消对象需要先澄清，不能猜测 ID。

---

## 时间规则

创建参数与[既有提醒模型](../../architecture/data/assistant-storage.md#提醒规则)对应，所有嵌套字段均有实际 Schema 说明：

```json
{
  "content": "检查本周待办，根据实际结果决定是否通知用户。",
  "schedule_type": "calendar",
  "schedule_config": {
    "frequency": "weekly",
    "start_date": "2026-10-26",
    "local_time": "09:00:00",
    "weekdays": [1]
  },
  "timezone": "Asia/Shanghai"
}
```

| 类型 | 参数 | 排期语义 |
| --- | --- | --- |
| once | at | 带 UTC 偏移的准确时间；创建时必须在未来，只触发一次。 |
| interval | start_at、every_seconds | 从固定起点按正整数秒推进；起点在过去时从下一个未来周期开始，不补创建前的事件。 |
| calendar | frequency、start_date、local_time，加适用的 weekdays 或 day_of_month/missing_day | daily/weekly/monthly，按指定 IANA 时区的当地日历排期。 |

时间计算位于 [reminder_schedule.py](../../../app/service/reminder_schedule.py)。程序计算 next_fire_at，数据库保存 UTC，结果按提醒时区展示。end_at 包含截止时刻，不设置则无预设截止；没有未来触发机会时拒绝创建。

- 每月缺失日期必须明确 skip 或 last_day，分别跳过该月或取月末。
- 周一为 1，周日为 7；周一至周五不自动解释为法定工作日。
- 夏令时不存在的当地时刻跳过；重复时刻只使用第一次，避免同一日历事件触发两次。
- 运行停机导致多个周期已过时，每条提醒合并产生一次补发，再计算严格晚于本次扫描时间的下一次时刻；不逐个补发旧周期，不随实际处理时长漂移。
- 到期即允许入队，不因助理忙碌跳过提醒。中间件保持提醒优先级，助理仍在当前任务结束后领取下一条。

---

## 事务与消息交付

复用 schema/011 的 assistant_reminders、assistant_deliveries，不新增表或迁移。已有助理消费仍要求 schema/012；应用不会自动执行 DDL。

1. 创建工具只产生经验证的候选提醒 ID 和排期；删除工具先读取本人记录。两者都不在 executor 中写库。
2. [助理仓库提交钩子](../../../app/repository/assistant_runtime.py)将已接受工具对交给[提醒仓库](../../../app/repository/assistant_reminder.py)，在同一节点事务里写入或取消提醒；异常、超时和取消与节点一起回滚。
3. 后续检查点重读已接受创建动作时，按相同 ID 验证不变字段，不能重置已经推进、完成或删除的提醒。删除保留 cancelled 记录及已有外键，不物理清除交付历史。
4. [调度器](../../../app/service/assistant_reminder.py)扫描到期记录，使用数据库行锁和 SKIP LOCKED；插入本次交付记录与推进 next_fire_at 在同一事务完成。唯一 reminder_id/scheduled_for 防止同次发生重复物化。
5. 事务提交后，[交付服务](../../../app/service/assistant_delivery.py)按记录的 task_type 发布 reminder，接收方固定为该提醒所属用户；不填助理通信的 source_assistant_id，不使用用户登录凭据。
6. 发布重试使用同一条交付记录的稳定 request_id 和正文。成功保存回执，清除 next_attempt_at；失败不重新排一次提醒。结果不明只在配置的幂等重试窗口内重试，窗口外保留待核对，不用新请求 ID 重发。

一次性提醒在本次交付记录物化后变为 completed，表示排期结束，不表示中间件已接受或用户已收到通知。暂停状态的历史记录不参与扫描；停用助理的排期及待发送提醒暂不发布。

删除与到期扫描竞争同一提醒行锁：删除先提交则不再产生该次事件；扫描先提交则本次交付已经冻结，删除只阻止未来事件，不能撤回已生成的交付、已入队任务或已发送消息。调度不使用消费者持有的整轮用户锁，因此当前生成不会阻止提醒入队。

---

## 生命周期与配置

在 [app/main.py](../../../app/main.py) 的助理启用分支装配。先检查表结构，再启动调度；退出先取消并等待调度及消费者，再关闭队列、机器人、平台会话和数据库资源。失败日志仅记录异常类型，不打印提醒正文、SQL 凭据或访问令牌。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| ASSISTANT_ENABLED | false | 同时启用任务消费、消息交付和已有提醒的到期调度。 |
| ASSISTANT_REMINDER_POLL_SECONDS | 3 | 扫描与尝试发送之间的轮询间隔。 |
| ASSISTANT_REMINDER_BATCH_SIZE | 100 | 每轮最多物化的提醒数量、最多读取的待发送数量。 |
| ASSISTANT_REMINDER_RETRY_SECONDS | 30 | 每次尝试前持久记录的下一次可重试时间，避免故障时紧密重发。 |
| ASSISTANT_PUBLICATION_RETRY_WINDOW_SECONDS | 3600 | 结果不明时允许自动重试的窗口，必须小于中间件幂等回执保留期。 |

配置声明在 [assistant_runtime.py](../../../app/config/assistant_runtime.py)，示例在 [.env.example](../../../.env.example)。不会修改实际 .env，不因导入工具启动后台工作。数据库读取失败停止本轮调度并稍后重试，不发布空表、不虚构提醒发送成功。

---

## 验证

[时间、工具和生命周期测试](../../../tests/test_reminder_tools.py)覆盖实际 Schema、时区与日历规则、静默策略边界、未来时间校验、分页、可折叠页面和关闭清理。

[独立 MySQL 测试](../../../tests/test_reminder_mysql.py)覆盖真实 Core 创建/查看/删除、归属与大小写隔离、节点事务失败和取消回滚、并发扫描、漏期合并、删除竞态边界、忙碌助理仍可入队、发布回执丢失和稳定键重试。中间件、机器人和模型使用替身；所有临时文件在 tests/tmp，不修改真实数据库或实际配置。

2026-10-24：新增 15 项时间/工具/生命周期测试及 14 项独立 MySQL 测试全部通过；连同原有消费链路专项共 46 项通过。完整 unittest 共 1247 项，1085 项通过、162 项因其他隔离环境未配置而跳过，包含实际执行的 47 项助理相关 MySQL 测试。`pip check`、`git diff --check` 及本轮 8 份文档的 194 个相对链接/锚点检查通过。临时容器和本轮日志已清理，实际 `.env`、共享服务及其他项目未修改；未进行真实中间件或钉钉消息发送。
