# 钉钉单聊机器人接入

## 用途与当前边界

使用企业内部应用机器人作为个人助理的单聊入口和回复出口：Stream 接收消息，OpenAPI 发送完整回复及主动提醒。一个应用机器人服务多位员工，由企业员工 `userid` 定位各自唯一助理；助理数据结构见[助理任务与提醒存储](../../architecture/data/assistant-storage.md)。

本阶段已提供配置、单聊消息规范化、异步 OpenAPI、Stream 生命周期和可靠接收器注入接口。OpenAPI 发送已供[助理消费者](../application/assistant-consumption.md)的交付服务使用，ASSISTANT_ENABLED 默认 false。**Stream 已挂到 FastAPI 启动流程，通过[可靠接收器](../application/assistant-inbound.md)整条校验、暂存并发布中间件任务；已有真实入站及助理执行记录，最终转换与交付的验证边界见[真实任务观察](#真实任务观察与验证边界)。** 只有 ASSISTANT_ENABLED=true 才启动，单纯填写凭据不会启动 Stream。现有 H5 登录适配保持原有行为，机器人不会创建中央用户登录或覆盖已有权限；未建档员工的首条有效消息会复用网页登录初始化服务，创建真实员工和初始等级 0 的文件权限。

采用钉钉公开 [Stream 协议](https://open-dingtalk.github.io/developerpedia/docs/learn/stream/protocol/)及[官方 Python SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)核对帧格式；传输复用本项目 `httpx` 和已安装的 `websockets==16.1.1`，使用原生异步 I/O，统一代理、重定向和脱敏边界，不引入 SDK 中的同步 HTTP 便捷方法。机器人 OpenAPI 的请求与回执字段另核对官方 [alibabacloud-dingtalk 2.2.65](https://pypi.org/project/alibabacloud-dingtalk/2.2.65/) 的 `robot_1_0` 模型。

---

## 钉钉后台与配置

后台创建或选择企业内部应用，开启机器人能力，接收方式选择 Stream，设置可见范围并发布；配置单聊发送和文件下载所需权限；首次员工建档另需成员信息读权限及部门信息读取权限，通讯录授权范围须覆盖目标员工及所属部门。可见范围内的用户通过机器人入口发起单聊。Stream 无需公网回调地址，但运行环境必须能访问钉钉 HTTPS/WSS 服务。参考[应用机器人](https://open-dingtalk.github.io/developerpedia/docs/learn/bot/appbot/overview/)和 [Stream 介绍](https://open-dingtalk.github.io/developerpedia/docs/learn/stream/overview/)。

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
| `send_file(userid, filename, content)` | 上传 PDF 媒体后使用 `sampleFile` 分别发送文件，单份不超过 20 MiB。 |
| `download_file(SecretStr(download_code))` | 换取临时地址并返回文件字节。 |
| `open_stream()` | 获取一次性连接地址，返回 SecretStr，不返回可直接打印的明文凭据。 |
| `close()` | 清理 token 和 HTTP 客户端。 |

发送请求为 `POST /v1.0/robot/oToMessages/batchSend`，携带钉钉应用 token、`robotCode`、单个目标的 `userIds`、`msgKey` 和 JSON 字符串 `msgParam`。应用 token 来自 `/v1.0/oauth2/accessToken`，并发获取合并、提前刷新；它与本项目中央用户 token 无关。接口出处见[单聊发送](https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches)。

成功必须有 `processQueryKey`，且 `invalidStaffIdList`、`flowControlledStaffIdList`、`filteredStaffIdList` 不包含失败目标；HTTP 200 本身不代表成功。该回执只表示接口受理，不代表用户已读。正文不自动截断或拆成多条，超限等上游拒绝需交付层显式处理。

文件发送先调用官方[上传媒体文件](https://open.dingtalk.com/document/orgapp/upload-media-files)：`POST https://oapi.dingtalk.com/media/upload`，query 携带应用 `access_token`，multipart 字段为 `type=file` 与 `media`。成功取得 `media_id` 后，使用 `sampleFile`，`msgParam` 包含 `mediaId`、`fileName`、`fileType=pdf`。当前仅发送系统保存的 PDF 用户附件；业务范围与事务边界见[最终回复附件交付](../application/assistant-consumption.md#最终回复的用户附件交付)。上传错误表示尚未发送用户消息；发送阶段仍遵循结果不明不重发的规则。上传响应限制为 1 MiB，凭据及媒体内容不写入日志。

客户端不自动重试发送。401 仅失效本次使用的应用 token，下次调用重新获取；超时、5xx 或无法解析的发送成功响应通过 `RobotUnavailable.uncertain=True` 表示结果不确定，交付层不能立即盲发第二条。当前发送接口没有为本适配器提供可用的客户端去重键，不承诺网络发送恰好一次；后续交付恢复需明确查询/人工处理等策略。

普通回复和主动提醒统一走 OpenAPI，不依赖有过期时间的 `sessionWebhook`，也不通过 Stream 回传最终答案。官方区别见[发送方式](https://open-dingtalk.github.io/developerpedia/docs/learn/bot/appbot/reply/)。

---

## 附件与日志边界

`download_file()` 使用 `robotCode + downloadCode` 调用 `/v1.0/robot/messageFiles/download` 获取 `downloadUrl`。仅通过 HTTPS 下载：钉钉返回 HTTP 时，只对已在允许列表内、默认 HTTP 端口的标准 OSS 桶域名（`<bucket>.oss-<region>.aliyuncs.com`）升级为 HTTPS，原始路径和签名查询字节保持不变；其他 HTTP 地址仍拒绝。OSS 标准桶域名支持 HTTPS，依据[阿里云官方说明](https://www.alibabacloud.com/help/en/oss/user-guide/access-oss-via-bucket-domain-name)。升级后仍只接受允许域名的 HTTPS 公网地址，DNS 结果经已有公开地址解析器校验后固定连接 IP，同时保留正确的 TLS SNI 和 Host；不跟随重定向，不使用环境代理，不向下载目标转发应用 token 或 Cookie。读取在总超时和字节上限内完成，空文件和超限文件失败。接口出处见[下载文件](https://open.dingtalk.com/document/orgapp/download-the-file-content-of-the-robot-receiving-message)。

`downloadCode` 仅以 SecretStr 驻留当前规范化对象，常规序列化/日志不包含它；因此后续可靠接收器不能只保存 `model_dump()` 就确认附件已接收。必须在确认前取回文件并可靠交给中间件，或设计完整受保护的接收恢复流程。[业务接收器](../application/assistant-inbound.md)确认前完成可靠暂存；临时下载码不落库，也不使用用户原文件名作为磁盘路径。

错误只向上返回稳定操作类别、可选 HTTP 状态和发送不确定标记，不回显上游正文、签名 URL 或凭据。WebSocket 底层日志关闭；HTTPX/httpcore 日志仅在本机器人请求的异步上下文内过滤，避免 INFO 日志泄漏临时下载链接，不影响其他模块 HTTP 日志。宿主也不得自行打印原始推送帧、下载码或 token。

接收处理中的 `RobotUnavailable` 日志包含 `operation`、`status`、`reason` 和 `uncertain`。文件下载仍使用 `operation=download`，通过固定原因前缀区分 `download_url`（下载地址校验）、`download_dns`（解析与公网地址校验）、`download_http`（连接和响应状态）、`download_body`（正文读取）。HTTP 下载失败保留状态码；网络和解析错误仅记录异常类别，不记录异常原文或实际链接。地址校验另区分 `missing_url`、`scheme`、`missing_host`、`userinfo`、`fragment`、`port`、`host_not_allowed`；拒绝日志仅附带 `download_host` 和 `download_scheme`，不记录路径、签名查询或完整地址。应用 token 失败单独标记 `operation=token`。网络故障仍使 Stream 返回失败，不把暂时无法下载谎报为图片损坏或成功接收。

---

## 验证与下一步

2026-10-26 下载诊断扩展：[故障与日志测试](../../../tests/test_robot_download_diagnostics.py)4 项通过，覆盖下载接口/文件 HTTP 状态、重定向拒绝、域名策略、DNS、公网地址限制、连接及读取超时、空文件、正常字节和 Stream 脱敏日志。机器人相关测试共 33 项通过，`git diff --check` 通过。另以当前配置实际获取应用 token 成功；没有发送消息、读取用户图片或保存 token，图片下载故障仍需真实重发后的具体阶段日志确认，不能据应用 token 成功推断附件下载权限或文件链接一定可用。

[HTTP 与输入测试](../../../tests/test_dingtalk_robot.py)覆盖可信单聊、企业/机器人过滤、富文本顺序、附件引用、精确请求体、token 并发刷新、401、拒收名单、不确定发送、下载域名/DNS/大小与日志脱敏。[Stream 测试](../../../tests/test_dingtalk_stream.py)覆盖 ACK 时机、超时、重推、系统心跳、容量限制、断线重连及取消清理，并使用本机随机端口 WebSocket 验证真实收发帧。HTTP 均为 MockTransport；配置样本在 `tests/tmp/`，不用实际 `.env`、共享服务或真实附件。

2026-10-24 验证：新增 36 项全部通过；完整 unittest 共 1141 项，969 项通过、172 项因未配置其他隔离数据库/插件环境而跳过。`pip check`、`git diff --check` 和本轮相关文档 91 个本地文件链接检查通过。没有调用真实钉钉、中间件或模型，也没有修改实际配置或业务数据。

可靠接收、队列发布、助理执行和最终交付现已装配，入站隔离验证见[入站专题](../application/assistant-inbound.md)。部署启用前仍需独立核对应用可见范围、权限、企业用户映射、文件下载域名以及真实消息限额；上述隔离测试本身不证明真实联通；后续真实运行记录单独列于下节。

2026-10-26 地址校验细分：根据真实重发日志，失败发生在下载地址校验阶段，尚未进入 DNS 或图片读取；当前日志不足以区分域名与其他地址条件，不能据此放宽域名策略。新增地址拒绝分类及域名/协议诊断，2 项测试覆盖缺失地址、HTTP、非授权域名、用户信息、端口、片段与签名链接脱敏。机器人相关回归共 35 项通过，`git diff --check` 通过；实际地址校验失败原因仍待下一次重发日志确认。

2026-10-26 实际故障定位与修复：用户重发日志确认钉钉返回 HTTP 的标准 OSS 桶下载地址，命中 `download_url:scheme`。已按上述规则升级为 HTTPS，保留域名允许列表、公网 DNS 校验、固定 IP、TLS SNI、拒绝重定向及大小限制。新增 2 项测试验证 HTTP/80 升级、签名路径和重复查询参数保真、请求头不携带应用凭证，以及伪造域名、非标准端口与配置排除继续拒绝。机器人相关回归共 37 项通过，`git diff --check` 通过。实际 OSS 域名的 HTTPS/TLS 探测成功，无签名根路径返回预期的 HTTP 403；未读取用户图片，真实带签名附件的完整收发仍待重发确认。


---

## 真实任务观察与验证边界

2026-10-30（北京时间），James 的一条真实消息经 Stream 接收、发布中间件队列并被助理领取，主模型已完成检索并生成 `finish_task` 调用；正文转换阶段报 `MLLMUnavailableError`，任务记录为 failed 且无交付记录。该记录证明入站、队列和主生成链路实际运行过，不能证明正文转换或最终钉钉交付成功。

同日 10:52:51+08:00 的诊断及复查观察到模型端口拒绝连接，无 HTTP 响应；这只是当时的服务状态，不表示后续持续不可用。历史转换错误已被统一封装，现有证据不足以断言其原因是 Schema、原生推理、OOM 或 300 秒请求超时。

证据见[诊断分析](../../../experiment/assistant-rendering-diagnosis/20261030-james/analysis.md)、[任务审计投影](../../../experiment/assistant-rendering-diagnosis/20261030-james/task-evidence.json)和[合成探测结果](../../../experiment/assistant-rendering-diagnosis/20261030-james/probe-results.json)。实验产物依项目白名单规则不随部署副本分发。完整转换与交付验收需在依赖可用时单独完成，不以这次失败记录或早期隔离测试替代。


---

## 单次引用回调诊断

`app/infrastructure/robot_reply_capture.py` 提供显式开启的脱敏采样，调用位置在 Stream 解码后、业务解析前。默认不存在启用文件时不采集；不启动额外 Stream 连接，不改变接收或 ACK，诊断异常只记录异常类型。

启用文件为 `experiment/dingtalk-reply-capture/armed.json`，包含目标 `userid`、仅小写字母/数字/连字符的 `run` 目录名及带时区的 `expires_at`。仅采集有效期内该用户下一条可信企业单聊，写入对应运行目录的 `callback.json`；以独占创建保证重推及后续消息不覆盖首次证据。移除启用文件即可关闭，到期自动失效。源码首次接入需要重启，后续读取启用文件不需要重启。

产物保留引用字段结构及白名单内的消息关联、类型和正文值；采样格式 2 另保留 `originalProcessQueryKey`、`elementType` 和 `cardContent` 内 `elementType=TEXT` 节点的 `value`，用于对照发送回执及原文。卡片非文字节点的 `value` 继续脱敏，不放开图片链接和按钮参数。下载码、Webhook、身份字段和其他未知标量只保留存在性和类型。最多12层、每对象100字段、每列表30项、单字符串4000字符，不得把它称为完整原始报文。敏感凭据不进入产物或日志，采样结果不进入模型上下文。若引用信息未出现，应区分平台本次未提供与采样截断，结合实际字段判断，不能从规范化消息反推原始回调。

2026-11-09：13 项采样与接收回归测试通过，覆盖目标过滤、嵌套敏感字段脱敏、引用正文保留、过期和首次证据保护。随后真实采样已确认图片引用位于 `text.repliedMsg`，包含原消息 ID、`msgType=picture` 和下载码字段，顶层 `originalMsgId` 与引用 ID 一致；下载码值未保存。采样已关闭。引用文字、图片、图文及助理文字卡片的正式处理与本地文件复用见[引用回复作为新任务](../application/assistant-inbound.md#引用回复作为新任务)。

---

## HTTP 固定出口代理

`DINGTALK_PROXY_URL` 为可选 HTTP/HTTPS 代理地址，留空直连；可包含代理认证信息，应按密钥管理。配置由 `AuthSettings`、`DingTalkRobotSettings`、`AvatarSettings` 统一校验，环境变量覆盖 `.env`。不支持 SOCKS、路径、查询或片段。配置不合法会拒绝加载，连接失败不回退直连。

`DingTalkHTTPClient` 显式传入代理、`trust_env=False`、`verify=True`、`follow_redirects=False`，忽略系统代理并保持目标证书验证。仅抑制自身请求上下文内 HTTPX/HTTPCore 日志，避免代理凭据、应用 token 和签名 URL 出现在底层日志。业务异常继续按固定类型和脱敏状态反馈。

| 链路 | 代理覆盖 |
| --- | --- |
| 应用 token、免登 code、员工/部门、日程 API | 覆盖；日程继承通用钉钉客户端。 |
| 机器人发送、媒体上传、下载地址获取 | 覆盖。 |
| Stream `/gateway/connections/open` HTTP 请求 | 覆盖。 |
| Stream WebSocket 长连接 | 不覆盖，仍显式 `proxy=None`，不使用系统代理。 |
| 机器人附件与头像 CDN 下载 | 覆盖；独立客户端不带应用鉴权头，现有域名/重定向/大小校验保留。 |

固定出口代理需要放行钉钉 API 以及实际附件/头像 CDN 域名。若代理只放行 `api.dingtalk.com` 和 `oapi.dingtalk.com`，API 可用不代表附件或头像下载可用；被代理拒绝时不会直连绕过。机器人下载保留本地公网 DNS 检查，但代理端独立解析目标，因此出口侧也必须限制目标域名/地址。中间件、模型、网页搜索和注册中心请求不受此配置影响。

本机验证可仅对诊断进程设置 `DINGTALK_PROXY_URL=http://127.0.0.1:18888`；应用进程需重启后加载变更。本项目不建立隧道、不配置代理服务器，也不修改共享部署。隔离测试：`PYTHONPATH=tests python -m unittest test_dingtalk_proxy -q`，覆盖配置校验与脱敏、环境覆盖、CONNECT 转发、代理故障无直连回退及底层日志脱敏。

2026-11-11 本机联调：仅为诊断进程设置上述回环代理，使用本项目 `DingTalkClient` 与本地凭据获取应用 token（0.874 秒）及读取根部门（0.927 秒）均成功。未保存或输出 token、凭据和人员资料，未发送消息，未启动后台同步或 Stream。代理/根部门/下载诊断/日程/无损头像相关隔离测试共 33 项通过；该轮未验证 WebSocket 或 CDN 经代理可用，当时临时代理只放行 API 域名。

同日本机下载补验：协作方将临时代理放行范围扩展为 `dingtalk.com`、`alicdn.com`、`aliyuncs.com` 本域及子域的 443，与应用默认下载域名一致。通过代理只读获取 James 的头像地址，仅记录域名 `static-legacy.dingtalk.com`；使用 `AvatarStore.save_from_url` 完成头像下载、无损编码和临时文件保存，用时 3.810 秒，随后清理测试临时目录。未写入 `data/`，未记录头像 URL 或用户资料，未发送消息。此结果仅验证该头像域名及样本；机器人下载码换取附件、其他 CDN 域名和重定向链仍未真实联调，WebSocket 仍直连。自定义下载白名单与实际返回域名如超出上述范围，需单独核对代理策略，不能据单个头像成功声称所有文件链路已验证。
