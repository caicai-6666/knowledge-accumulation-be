# 钉钉单聊机器人接入

## 用途与当前边界

使用企业内部应用机器人作为个人助理的单聊入口和回复出口：Stream 接收消息，OpenAPI 发送完整回复及主动提醒。一个应用机器人服务多位员工，由企业员工 `userid` 定位各自唯一助理；助理数据结构见[助理任务与提醒存储](../../architecture/data/assistant-storage.md)。

本阶段已提供配置、单聊消息规范化、异步 OpenAPI、Stream 生命周期和可靠接收器注入接口。OpenAPI 发送已供[助理消费者](../application/assistant-consumption.md)的交付服务使用，ASSISTANT_ENABLED 默认 false。**Stream 已挂到 FastAPI 启动流程，通过[可靠接收器](../application/assistant-inbound.md)整条校验、暂存并发布中间件任务；未进行真实钉钉收发。** 只有 ASSISTANT_ENABLED=true 才启动，单纯填写凭据不会启动 Stream。现有 H5 登录适配保持原有行为，机器人不会创建中央用户登录或修改权限。

采用钉钉公开 [Stream 协议](https://open-dingtalk.github.io/developerpedia/docs/learn/stream/protocol/)及[官方 Python SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)核对帧格式；传输复用本项目 `httpx` 和已安装的 `websockets==16.1.1`，使用原生异步 I/O，统一代理、重定向和脱敏边界，不引入 SDK 中的同步 HTTP 便捷方法。机器人 OpenAPI 的请求与回执字段另核对官方 [alibabacloud-dingtalk 2.2.65](https://pypi.org/project/alibabacloud-dingtalk/2.2.65/) 的 `robot_1_0` 模型。

---

## 钉钉后台与配置

后台创建或选择企业内部应用，开启机器人能力，接收方式选择 Stream，设置可见范围并发布；配置单聊发送和文件下载所需权限。可见范围内的用户通过机器人入口发起单聊。Stream 无需公网回调地址，但运行环境必须能访问钉钉 HTTPS/WSS 服务。参考[应用机器人](https://open-dingtalk.github.io/developerpedia/docs/learn/bot/appbot/overview/)和 [Stream 介绍](https://open-dingtalk.github.io/developerpedia/docs/learn/stream/overview/)。

[DingTalkRobotSettings](../../../app/config/dingtalk_robot.py)独立读取机器人配置，不加载开发模拟身份；环境变量优先于 `.env`，不进行变量插值。凭据允许暂时留空，真正构造客户端或 Stream 时在本地严格校验。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `DINGTALK_CORP_ID` | 空 | 只接受该企业内部员工的单聊。 |
| `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` | 空 | 复用已有钉钉应用凭据，用于应用 token 和 Stream 建连。 |
| `DINGTALK_ROBOT_CODE` | 空 | 同一应用的机器人编码，显式填写，不猜测等于 AppKey。 |
| `DINGTALK_TIMEOUT_SECONDS` | 10 | HTTP 操作及连接握手/关闭超时，复用已有项。 |
| `DINGTALK_ROBOT_CALLBACK_TIMEOUT_SECONDS` | 10 | 可靠接收器最长等待秒数，不用于模型执行。 |
| `DINGTALK_ROBOT_RECONNECT_MAX_SECONDS` | 60 | 断线指数退避的最大秒数，包含随机抖动。 |
| `DINGTALK_ROBOT_MAX_PENDING_CALLBACKS` | 16 | 每条连接待确认的并发回调上限。 |
| `DINGTALK_ROBOT_MAX_FRAME_BYTES` | 1048576 | 单个 Stream 帧大小限制。 |
| `DINGTALK_ROBOT_MAX_IMAGE_PIXELS` / `DINGTALK_ROBOT_MAX_IMAGE_FRAMES` | 40000000 / 100 | 图片转 PDF 的单帧像素数及最大帧数。 |
| `DINGTALK_ROBOT_MAX_DOWNLOAD_BYTES` | 52428800 | 单附件下载大小上限。 |
| `DINGTALK_ROBOT_DOWNLOAD_ALLOWED_HOSTS` | `dingtalk.com,alicdn.com,aliyuncs.com` | 允许下载的域名及其子域，逗号分隔。 |

示例位于 [.env.example](../../../.env.example)，本次未修改真实 `.env`。Stream 与助理消费共用 ASSISTANT_ENABLED，默认 false；不提供公开 HTTP 回调路由。独立宿主仍可注入可靠接收器并调用 `run()`，应用通过 `start()` 管理后台任务。

---

## 接收与可信身份

[parse_robot_message](../../../app/model/dingtalk_robot.py)只接受来自本适配器已认证 Stream 连接的数据，不能直接当作公网 HTTP 请求的鉴权函数。

| 入站字段 | 处理 |
| --- | --- |
| `conversationType` | 仅字符串 `"1"` 进入业务接收器；群聊等其他类型确认忽略。 |
| `chatbotCorpId` / `senderCorpId` | 均必须匹配配置企业。 |
| `robotCode` | 必须匹配配置机器人。 |
| `senderStaffId` | 映射为用户 `userid`；缺失时拒绝，不回退到昵称或加密 `senderId`。 |
| `msgId` | 业务消息 ID，生成稳定入队去重键。 |
| `conversationId` | 保留来源会话，发送仍由宿主绑定的可信用户决定。 |
| `createAt` | 毫秒时间戳，转换为 UTC。 |
| `msgtype` / 消息正文 | 规范化为按原顺序排列的 `parts`，保留文本与附件交错。 |

规范化消息支持文本、图片、富文本、文件、语音和视频的**输入引用**。这不表示 Core 已支持所有媒体：当前查看能力仍是 PDF 多图。[业务接收器](../application/assistant-inbound.md)已实现图片转 PDF、现有 PDF 校验以及不支持文件的整条拒绝反馈；音视频不进入 Core。本层不悄悄将不认识的富文本片段丢弃后继续入队。

消息中的 `sessionWebhook`、昵称和 `isAdmin` 不作为认证或授权依据，不进入规范化消息。可信企业身份也不自动等于文件访问权限，后续助理执行仍需读取权威员工权限；此接入不会签发、恢复或替代中央用户 token。

---

## Stream 确认与生命周期

[DingTalkRobotStream](../../../app/infrastructure/dingtalk_stream.py)通过 `POST /v1.0/gateway/connections/open` 只订阅机器人 CALLBACK topic `/v1.0/im/bot/messages/get`，使用返回的一次性 ticket 建立 WSS。每次重连重新获取 ticket，不复用或保存它。端点必须是钉钉 HTTPS 对应的 WSS 子域，禁止用户信息、额外端口或重定向；WebSocket 不使用环境代理。

接收器接口为 `async receive(message: RobotMessage) -> bool`：

1. 按 `message.request_id` 完成持久去重及可靠接收。该键由企业、机器人和业务 `msgId` 组合产生，同一消息不同推送帧保持一致。
2. 只有可靠入队或可靠暂存完成后才返回严格布尔 `True`。返回 None/False、抛异常、超时均返回 500 协议确认，不伪称接收成功。
3. `headers.messageId` 是推送帧 ID，只用于该帧 ACK；它与业务 `msgId` 不同。

同一业务消息并发重推共用正在进行的接收调用，但本层**不提供持久去重**，重启或后续重推仍会交给接收器。回执丢失时外部可能已经提交，因此接收器必须具备稳定请求键的幂等能力；不能用内存缓存宣称恰好一次入队，也不把回调失败视为钉钉一定会无限重推的保证。

系统 `ping` 单独回应，不被慢接收器阻塞；`disconnect` 回应后重连。达到并发上限时返回失败，不无限堆积任务。连接断开及停止时取消并等待未结束回调，任何已提交但未确认的业务由接收器负责重放去重。这里的连接心跳与助理“空闲唤醒任务”无关。

```python
from app.config.dingtalk_robot import load_dingtalk_robot_settings
from app.infrastructure.dingtalk_stream import DingTalkRobotStream

async def run_robot(receive):
    # receive 由宿主提供，必须在可靠保存/入队后才返回 True。
    stream = DingTalkRobotStream(load_dingtalk_robot_settings(), receive)
    try:
        await stream.run()
    finally:
        await stream.close()
```

`run()` 防止重复启动，`stop()` 取消并等待运行任务，`close()` 还关闭自建 HTTP 客户端。外部注入的 `client` 由宿主关闭。构造对象不发起网络请求，不创建上传目录或写入文件。

---

## 完整回复与主动提醒

[DingTalkRobotClient](../../../app/infrastructure/dingtalk_robot.py)提供：

| 方法 | 行为 |
| --- | --- |
| `send_text(userid, content)` | 使用 `sampleText` 向指定员工发送完整文本。 |
| `send_markdown(userid, title, text)` | 使用 `sampleMarkdown` 发送完整 Markdown。 |
| `download_file(SecretStr(download_code))` | 换取临时地址并返回文件字节。 |
| `open_stream()` | 获取一次性连接地址，返回 SecretStr，不返回可直接打印的明文凭据。 |
| `close()` | 清理 token 和 HTTP 客户端。 |

发送请求为 `POST /v1.0/robot/oToMessages/batchSend`，携带钉钉应用 token、`robotCode`、单个目标的 `userIds`、`msgKey` 和 JSON 字符串 `msgParam`。应用 token 来自 `/v1.0/oauth2/accessToken`，并发获取合并、提前刷新；它与本项目中央用户 token 无关。接口出处见[单聊发送](https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches)。

成功必须有 `processQueryKey`，且 `invalidStaffIdList`、`flowControlledStaffIdList`、`filteredStaffIdList` 不包含失败目标；HTTP 200 本身不代表成功。该回执只表示接口受理，不代表用户已读。正文不自动截断或拆成多条，超限等上游拒绝需交付层显式处理。

客户端不自动重试发送。401 仅失效本次使用的应用 token，下次调用重新获取；超时、5xx 或无法解析的发送成功响应通过 `RobotUnavailable.uncertain=True` 表示结果不确定，交付层不能立即盲发第二条。当前发送接口没有为本适配器提供可用的客户端去重键，不承诺网络发送恰好一次；后续交付恢复需明确查询/人工处理等策略。

普通回复和主动提醒统一走 OpenAPI，不依赖有过期时间的 `sessionWebhook`，也不通过 Stream 回传最终答案。官方区别见[发送方式](https://open-dingtalk.github.io/developerpedia/docs/learn/bot/appbot/reply/)。

---

## 附件与日志边界

`download_file()` 使用 `robotCode + downloadCode` 调用 `/v1.0/robot/messageFiles/download` 获取 `downloadUrl`。只接受允许域名的 HTTPS 公网地址，DNS 结果经已有公开地址解析器校验后固定连接 IP，同时保留正确的 TLS SNI 和 Host；不跟随重定向，不使用环境代理，不向下载目标转发应用 token 或 Cookie。读取在总超时和字节上限内完成，空文件和超限文件失败。接口出处见[下载文件](https://open.dingtalk.com/document/orgapp/download-the-file-content-of-the-robot-receiving-message)。

`downloadCode` 仅以 SecretStr 驻留当前规范化对象，常规序列化/日志不包含它；因此后续可靠接收器不能只保存 `model_dump()` 就确认附件已接收。必须在确认前取回文件并可靠交给中间件，或设计完整受保护的接收恢复流程。[业务接收器](../application/assistant-inbound.md)确认前完成可靠暂存；临时下载码不落库，也不使用用户原文件名作为磁盘路径。

错误只向上返回稳定操作类别、可选 HTTP 状态和发送不确定标记，不回显上游正文、签名 URL 或凭据。WebSocket 底层日志关闭；HTTPX/httpcore 日志仅在本机器人请求的异步上下文内过滤，避免 INFO 日志泄漏临时下载链接，不影响其他模块 HTTP 日志。宿主也不得自行打印原始推送帧、下载码或 token。

---

## 验证与下一步

[HTTP 与输入测试](../../../tests/test_dingtalk_robot.py)覆盖可信单聊、企业/机器人过滤、富文本顺序、附件引用、精确请求体、token 并发刷新、401、拒收名单、不确定发送、下载域名/DNS/大小与日志脱敏。[Stream 测试](../../../tests/test_dingtalk_stream.py)覆盖 ACK 时机、超时、重推、系统心跳、容量限制、断线重连及取消清理，并使用本机随机端口 WebSocket 验证真实收发帧。HTTP 均为 MockTransport；配置样本在 `tests/tmp/`，不用实际 `.env`、共享服务或真实附件。

2026-10-24 验证：新增 36 项全部通过；完整 unittest 共 1141 项，969 项通过、172 项因未配置其他隔离数据库/插件环境而跳过。`pip check`、`git diff --check` 和本轮相关文档 91 个本地文件链接检查通过。没有调用真实钉钉、中间件或模型，也没有修改实际配置或业务数据。

可靠接收、队列发布、助理执行和最终交付现已装配，入站隔离验证见[入站专题](../application/assistant-inbound.md)。部署启用前仍需独立核对应用可见范围、权限、企业用户映射、文件下载域名以及真实消息限额；当前测试不表示实际应用已发布或生产机器人已联通。
