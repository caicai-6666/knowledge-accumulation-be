# 助理消息队列接入

## 用途与当前范围

本模块对接中间件的助理任务发布、领取、续租、确认、状态查询及附件下载。复用已有平台会话，不建立第二份待领取队列，不把中间件的任务确认当作机器人结果发送。

实现位于 [HTTP 客户端](../../../app/infrastructure/assistant_queue.py)、[服务编排](../../../app/service/assistant_queue.py)、[请求响应契约](../../../app/model/assistant_queue.py)和[配置](../../../app/config/assistant_queue.py)。已提供可注入处理器的 `execute_once`；[后台消费与交付](../application/assistant-consumption.md)已接入任务账本、启动轮询和发送工具，默认关闭。[提醒调度](../application/assistant-reminders.md)已接入，[空闲心跳调度](../application/assistant-heartbeat.md)已接入，未执行真实机器人发送。持久数据结构见[助理存储](../../architecture/data/assistant-storage.md)。

助理初始上下文的局部读取见[助理记忆加载](../../architecture/workflow/question-answering/assistant-memory.md)，机器人收发边界见[钉钉单聊机器人](dingtalk-robot.md)。

---

## 中间件接口映射

以下接口均使用本项目的**平台 Bearer**；不传用户 access_token，不把来源平台写进请求体。中间件根据平台会话判定来源；调用者仍须保证 userid 与可信业务身份相符。

| 方法与路径 | 本地入口 | 行为 |
| --- | --- | --- |
| `POST /middleware-service/api/assistant/tasks/publish` | `publish` | 发布四种任务，支持文件；成功必须为 201。 |
| `POST /middleware-service/api/assistant/tasks/pull` | `pull` / `renew` | 仅传 userid；200 返回冻结任务及租约，204 才表示无任务。 |
| `POST /middleware-service/api/assistant/tasks/ack` | `acknowledge` | 传 userid、task_id、delivery_token；200 验证确认对象及任务 ID。 |
| `GET /middleware-service/api/assistant/tasks/status` | `status` | 查询参数 userid，读取执行中状态及上次心跳时间。 |
| `GET /middleware-service/api/resource/assistant-tasks/{file_id}` | `download` | 查询参数 file_extension；返回受大小限制的文件字节。 |

发布对象为 `userid/task_type/message/request_id/source_assistant_id`，任务类型为 `reminder/user_message/assistant_message/heartbeat`。本地强制要求稳定 request_id，尽管中间件允许省略。无附件发送 JSON；有附件发送 multipart，使用重复 `files`、`file_extensions` 字段，保持一一对应的原始顺序。空来源字段直接省略，不把 null 转为空表单值。不得在重试时重新生成 request_id 或变更消息、发送方助理、文件内容及排列。

`userid` 是接收方所属用户，`source_assistant_id` 是发送方助理标识，`source_platform_code` 是认证发布平台。发布和领取 `assistant_message` 均强制要求非空来源助理，最长 128 字符；缺失、null 或纯空白会明确失败，不从接收方、平台代码或消息正文推断身份，也不执行或自动确认来源不明的旧任务。其他任务允许无助理来源。中间件的强制必填校验由其维护方调整，本项目已按此最终契约执行。

本平台每位用户只有一个助理，发送适配以可信助理所属 userid 作为 source_assistant_id；模型只能选择接收方和消息，不能自报发送者。中间件转发的助理标识是来源平台声明的身份，不代表它已查询并认证该助理；来源平台与助理标识均须保留，不将来源身份当作接收用户的权限凭据。

中间件可合并连续用户短消息，甚至合并不同平台来源。客户端按本平台代码与 request_id 在 `user_messages` 中核验本次片段，并比较该片段的 source_assistant_id，不要求合并任务根对象来自本平台。根对象来源助理必须等于首条消息，各片段来源独立保留；助理消息不参与用户消息合并。实际执行以 pull 的冻结任务为准，不能用发布回执代替领取结果。重复发布回执可能仍含已经在 ack 后清理的附件引用，不代表文件重新创建。

所有响应执行严格 DTO 校验，拒绝错误用户、错误任务、伪造合并关系和不完整租约。未知字段、非布尔标记和不一致的忙碌状态也会失败。交付凭证采用 SecretStr，不进入普通序列化、日志和模型上下文。

---

## 租约与确认顺序

`execute_once(userid, handle)` 在同一服务实例内按用户串行，最多领取并处理一个任务；不同用户可并发。没有任务时返回 None，不调用处理器。执行期间按当前租约剩余时间的一半调用 pull 续租，只接受同一任务、同一 delivery_token 和有效新期限。

处理器 `handle(lease)` 必须依次完成以下事项后明确返回 `True`：

1. 可靠保存领取输入与任务处理状态，并将需要保留的附件转存到本地上传存储、登记到任务节点。
2. 执行或恢复本次任务，提交节点终态、完整结果和必要交付记录。
3. 若本次任务产生外发动作，完成对应交付，或已可靠交给具备恢复能力的持久发送器；没有外发动作的任务不伪造交付。

此后服务才调用 ack。中间件 ack 只确认消费并清理队列附件，既不保存模型答案，也不向钉钉转发。

### 任务结束与消息发送

助理 finish_task 必须显式传入 `send_to_user`：true 将最终结果交付给所属用户，false 只保存并结束。四类消息都使用这一规则；提醒先交给助理处理，不能直接推导为通知用户。助理间消息通过独立 send_assistant_message 发布，不依赖 finish_task，也不自动结束任务。完整工具及输入渲染契约见[助理消费与交付](../application/assistant-consumption.md#两个助理工具)。

节点终态与交付请求必须先在同一事务提交，随后才调用外部发送；当前消费者等待本轮所有交付完成后才 ack。Core 不直接调用机器人，探索模式的输出与 SSE 契约保持不变。

| 情况 | 处理 |
| --- | --- |
| 处理器异常、取消或返回值不是 True | 不 ack，取消并等待续租任务。 |
| 续租故障、过期、领取到另一任务或更换 token | 停止当前处理器，不执行或确认替代任务。 |
| ack 超时或 503 | 返回失败；可能已经确认但附件清理未完成。恢复方重试原 ack，不重新生成答案。 |
| publish 超时或响应契约错误 | 标记结果可能不确定；由调用方使用相同 request_id 和原内容重试。 |
| 平台 401 | 仅失效本次实际使用的平台 token，不清空其他任务或用户登录。 |
| 停机 | `close()` 取消并等待正在处理和等待本地锁的调用；不清空中央队列。 |

网络故障不自动重新执行任务；用户也没有打断当前助理任务的入口。因租约丢失停止是运行故障处理，后续依靠持久账本恢复。同平台多个消费者可能拿到同一租约，因此本地锁只约束同一个服务实例，**不保证跨进程恰好执行一次**。上层消费者另以同源 MySQL 命名锁、事务交付账本和发送原子抢占协调跨实例；具体故障恢复边界见[消费专题](../application/assistant-consumption.md#失败与恢复)。机器人结果不确定时仍不能盲目重发。

---

## 装配与配置

使用现有 `MiddlewareSessionService`、平台代码及中间件根地址注入 `AssistantQueueService`。客户端和服务均不自行登录平台或发平台心跳。宿主先关闭队列服务，再关闭其 HTTP 客户端，最后关闭共享平台会话；队列服务不关闭借入的资源。

```python
from app.config.assistant_queue import load_assistant_queue_settings
from app.infrastructure.assistant_queue import AssistantQueueClient
from app.service.assistant_queue import AssistantQueueService

client = AssistantQueueClient(str(middleware_settings.base_url),
                              settings=load_assistant_queue_settings())
queue = AssistantQueueService(platform_sessions, client,
                              platform_code=middleware_settings.platform_code)
try:
    receipt = await queue.execute_once(userid, reliable_task_handler)
finally:
    await queue.close()
    await client.close()
```

其中 `reliable_task_handler` 为宿主的可信处理器；应用实际装配 AssistantConsumer，启停配置见[消费专题](../application/assistant-consumption.md#数据库与启用配置)。发布方法同样是内部入口，不新增未经身份校验的 HTTP 发布接口。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `ASSISTANT_QUEUE_TIMEOUT_SECONDS` | 40 | 单次传输及完整响应读取超时；续租另受剩余租期约束。 |
| `ASSISTANT_QUEUE_MAX_FILES` | 10 | 一次发布的附件数量上限。 |
| `ASSISTANT_QUEUE_MAX_FILE_BYTES` | 20971520 | 单个上传/下载附件字节上限。 |
| `ASSISTANT_QUEUE_MAX_RESPONSE_BYTES` | 1048576 | JSON 回执大小上限。 |

配置从 `.env` 和进程环境加载，进程环境优先；示例维护在 [.env.example](../../../.env.example)。附件限制需与中间件部署配置一致；不自动转换用户文件格式。HTTP 不信任环境代理、不跟随重定向；下载返回字节，不直接写默认 data 目录。运行时应通过既有上传服务显式落盘到 `data/user/upload-file`，测试必须覆盖为 tests/tmp。

状态查询只提供中央事实；[空闲心跳调度器](../application/assistant-heartbeat.md)使用 `has_in_progress_task` 避免忙碌时发心跳，结合 `last_heartbeat_at` 与用户间隔控制频率；间隔 -1 的助理不发送心跳。

---

## 验证

[队列隔离测试](../../../tests/test_assistant_queue.py)使用 HTTP MockTransport 覆盖 JSON/multipart、合并片段、重复发布、严格响应、204、文件下载、平台凭据失效、续租、单用户串行、ack 不确定状态及取消清理。未访问真实中间件、Redis 或钉钉；不将隔离契约通过表述为生产联通。

2026-10-24 验证：本轮新增 36 项测试（队列及配置 26 项、局部父链 4 项、独立 MySQL 8.4 上下文 6 项）全部通过。携带临时测试库端口执行完整 unittest 共 1177 项，1015 项通过、162 项因其他隔离环境未配置而跳过；本轮 6 项及原有 10 项助理 MySQL 测试均实际执行。`pip check`、`git diff --check` 及本轮九份文档的 214 个相对链接/锚点通过。临时容器和测试输出清理，不修改真实 `.env`、业务数据或其他项目。

2026-10-24 来源助理契约补验：新增 6 项测试覆盖 JSON/multipart 来源传递、缺失和非法来源本地拒绝、发布回执来源不符、领取失败不执行/不确认、合并片段来源及续租来源不可变。队列与存储契约 42 项通过；完整回归 1183 项，1005 项通过、178 项因未配置隔离实库环境跳过。本次未重建 MySQL 容器，未修改 SQL 结构。依赖、差异格式及两份修改文档的 21 个相对链接/锚点检查通过，未调用真实中间件或机器人。
