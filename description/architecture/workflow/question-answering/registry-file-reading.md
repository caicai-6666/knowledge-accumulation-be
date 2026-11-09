# 注册中心文件图像查看

工具独立注册图像查看渲染钩子，正文为多行可读回执，保留注册中心文件 ID、当前页及总页数，按展示、隐藏、不可读三态选择。历史不保存图像正文，不回显凭据；既有鉴权、下载、缓存和提交后阅读位置语义不变。 渲染及兼容边界见[工具反馈契约](tool-content-contract.md#core-工具反馈迁移清单)。

## 用途与工具契约

`view_registry_file`读取注册中心已登记PDF，按页向模型展示原生图像。参数形式与用户附件的`view_image_file`一致，文件身份属于不同来源；不接受音频、视频、URL或本地文件路径。

| 参数 | 规则 |
| --- | --- |
| `file_id` | 必填十进制字符串，直接复制文件查询列表中的文件ID，范围1至18446744073709551615；不是JSON数字、来源平台`source_id`、上传附件UUID、文件名或路径。 |
| `page_number` | 可选，从1开始的文件顺序页码。省略/null首次第1页，之后本轮上次成功查看页的下一页；显式页码可重看，末页不循环。 |

先使用[文件查询及候选列表](file-retrieval.md)定位文件，或使用上下文中已经明确提供的正式资源ID；不得猜测ID。工具说明、参数校验和实际发送给模型的Schema使用同一约定。用户身份、access_token、服务地址均不是模型参数。

调用示例：

```json
{"file_id":"101","page_number":1}
```

成功返回`file_id/page_number/page_count`及轻量查看记录，内容类型为`foldable`。SSE 动作类型为 `thinking`，文案为 `正在查看文件：{name}`。`name` 取自当前用户权限校验通过的正式入库记录 `resources.name`，不是磁盘文件名、下载响应头、旧候选缓存或模型参数。名称展示合并空白、移除不可打印字符，总文案不超过 2000 字符，过长时加省略号；文件名包含冒号时仍保留，前端应按固定动作前缀后的第一个全角冒号分隔。名称为空、无法通过鉴权或内部宿主未提供名称时使用通用提示 `正在查看文件`，实际查看继续独立鉴权并保留原错误反馈；取消向上传播。工具回执不携带原件、图片base64或用户凭据；对应资料消息包含定位说明和一张原生`image_url`图片：

```text
文件来源: 注册中心
文件类型: 多图
文件ID: 101
页码: 1
总页数: 3

[本页原生图像]
```

图像为PDF页面的PNG渲染，不用OCR文字替换原页。模型应根据实际页面核实内容，需要长期保留的事实、来源和限制写入工作区；查看记录不能代替原文。

---

## 原件获取与服务身份

[RegistryFileClient](../../../../app/infrastructure/registry_file.py)只调用固定接口：

```text
POST {REGISTRY_BASE_URL}/knowledge-accumulation/registry/api/resource/service-authorized-file/{resource_id}
Authorization: Bearer <knowledge-accumulation平台token>
Content-Type: application/json

{"userid":"由可信任务宿主绑定的所属用户"}
```

缓存未命中时使用此接口下载，注册中心验证调用平台，只有 `knowledge-accumulation` 可以代表用户读取，并实时校验该员工对文件来源平台的访问等级。不要求用户保持网页登录或具有注册中心管理员身份。原用户 Bearer 的 GET 入口由注册中心保留兼容，本项目不再使用，也不在服务间鉴权失败时回退旧入口。不跟随重定向、不读取环境代理。

探索任务仍在 HTTP 入口校验中央用户登录，后台只保存可信 `AuthenticatedUser` 身份，不再把网页登录 Bearer 传入任务或文件查看器。个人助理从已绑定用户执行任务，因此机器人首次问答、定时任务和心跳不需要网页用户 token。模型工具参数仍只有文件 ID 和页码，不能自行选择代办用户。内部 Python 宿主同样必须提供可信用户身份，不可把模型生成的 userid 用作任务归属。

`RegistryFileClient` 每次下载从共享 `MiddlewareSessionService` 获取当前平台令牌，平台重登后自动使用新令牌；收到注册中心 401 时仅失效本次使用的平台令牌，由既有会话后台恢复。平台 token 不写入任务、SQL、模型上下文、工具回执或 SSE。助理与探索工厂继续共享下载客户端及 `RegistryFileAccessService`；停机先结束任务，再关闭下载客户端和平台会话。

注册中心通过中间件 `POST /middleware-service/api/platform/token/verify` 只读校验传入平台令牌：使用注册中心自身平台 token 认证校验请求，body 携带待校验 token。只接受 `valid=true` 且 `code=knowledge-accumulation`，故障拒绝；不通过心跳接口验证，不延长被验证平台的会话。完整服务间接口以注册中心与中间件各自 API 文档为准。

---

## PDF、安全读取与缓存

[RegistryFileViewer](../../../../app/agent/core/file/registry_viewer.py)复用[图像查看器](image-reading.md)的页码游标、`FileModel`、视觉预算和`FileModelCache`。应用已装配本平台鉴权时，以 `(用户、树、正式资源ID)` 定位内存模型，不为缓存命中下载PDF或计算内容哈希；上传UUID与正式整数ID不混淆。查看器按任务隔离，重开任务从第一页开始；LRU淘汰后才重新下载，本轮游标保留。

每次工具查看、翻页和驻留图片再次注入都执行以下流程：

1. 查看器使用 `(任务所属用户 userid, 树 ID)` 绑定身份，所有本地鉴权与远程下载都使用同一个 userid；不接受模型、文件候选或其他助理消息指定的用户身份。
2. 通过文件检索服务的 `read(userid, (resource_id,))` 直读权威SQL：同一只读快照中读取 `employees.platform_permissions` 和正式 `resources`。只比较文件实际来源平台对应的等级，缺平台键、未知文件等级、员工缺失或资源删除均拒绝。管理员列表和开发模拟配置不赋予文件权限，检索候选及向量缓存不参与判断。
3. 权限通过后取得LRU租约；命中直接渲染，未命中才带平台 Bearer 和绑定 userid 下载 PDF 并加载。渲染完成再次鉴权，权限变更或依赖故障不发布页面。鉴权失败不能回退为跳过校验的缓存读取或下载。

SQL配置沿用 `FILE_SOURCE_MYSQL_*`，必须指向registry实际维护的权威人员及资源表；不能接独立的旧副本，也不能只依赖当前任务身份中的权限快照。缓存命中按可信 userid 实时查权威 SQL，无需用户登录查询或重新下载；部署时需同步升级中间件只读平台令牌校验及注册中心服务间文件入口。

正式资源ID对应已入库的不可变原件，重新入库使用新ID；文件等级变化继续实时查SQL。直接在磁盘原地改写、删除原件而不更新资源登记不属于支持的更新方式，缓存命中不会检测这种带外操作。内部宿主若未装配本平台权威鉴权，保留每次远程授权下载及内容摘要校验路径，绝不能无条件复用；已装配鉴权但查询失败时不会退回该路径。

原件仅在内存处理，不写入`data/user/upload-file`，不复制注册中心磁盘文件。响应只接受200、`application/pdf`或`application/octet-stream`和identity编码；下载前检查声明长度、流式累计检查实际字节、核对完整长度及PDF签名，再由MuPDF校验真实PDF、页数和打开密码。不信任文件名或MIME来代替内容验收。

阅读位置和可解析引用只在工具结果提交成功后生效；失败不推进。引用与具体查看器、资源ID及页码绑定，伪造或跨任务引用不可读。关闭查看器清理游标和页面引用；关闭公共池时等待渲染租约归还，不强制释放正在使用的MuPDF对象。图像自动折叠沿用`AGENT_PAGE_DISPLAY_ROUNDS`，折叠后不继续读取原件。

---

## 失败边界

| 情况 | 工具反馈 |
| --- | --- |
| 无有效平台token | `platform_session_required`，不发送HTTP，由既有平台会话服务恢复。 |
| registry返回401 | `platform_session_expired`，失效本次平台token，不创建用户登录。 |
| registry返回403，或本地SQL不允许读取 | `file_forbidden`，不使用已缓存图像绕过授权。 |
| registry返回404 | `file_not_found`；本地SQL查询不到可读资源统一返回 `file_forbidden`，不区分不存在与无权。 |
| 平台身份非knowledge-accumulation | 注册中心拒绝，返回 `file_forbidden`，不改用其他身份重试。 |
| 本平台SQL查询故障或结果契约错误 | `file_authorization_unavailable`，不回退旧权限或重新下载。 |
| registry返回503、其他异常状态、网络或总超时 | `registry_unavailable`。 |
| 非PDF或损坏/加密PDF、长度/编码异常、下载超限 | 明确的格式、响应或大小错误，不发布页面。 |
| 页码越界、已到末页 | 不改变阅读位置，可显式指定有效页重看。 |
| 已展示页面再次读取失败 | 标记临时资料不可用，不回放旧缓存图像；模型需要时重新调用查看工具。 |

错误反馈不会包含上游错误正文、服务地址、认证头或底层异常内容。取消向上传播，不能伪装为普通失败后继续执行。

---

## 配置与验证

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `REGISTRY_BASE_URL` | 空 | HTTP/HTTPS根地址；不得携带路径、凭据、query或fragment。空值不阻止应用启动，调用工具时明确提示未配置。 |
| `REGISTRY_FILE_TIMEOUT_SECONDS` | 60 | 单次服务间下载整体超时，同时用于HTTP请求超时。 |
| `REGISTRY_FILE_MAX_DOWNLOAD_BYTES` | 52428800 | 原件下载最大字节数，可按部署资源调整。 |
| `AGENT_FILE_CACHE_MAX_FILES` | 沿用现有值 | 与上传附件共用PDF模型LRU容量。 |
| `AGENT_PAGE_DISPLAY_ROUNDS` | 沿用现有值 | 原生图片的完整响应展示轮次。 |

示例见[.env.example](../../../../.env.example)。本项目无需新增数据库表或依赖，不改文件查询结果集契约、不修改真实 `.env` 或前端。需要中间件、注册中心及本项目的配套代码均已加载后才能使用新路径。

[服务身份与缓存测试](../../../../tests/test_registry_service_identity.py)使用 MockTransport、内存合成 PDF 和权威查询替身，覆盖无网页登录的读取、准确 POST 路径与绑定 userid、平台 token 轮换、401 失效、403 拒绝、缓存权限复核、跨用户隔离、SQL 故障、渲染期间撤权、模型参数边界及取消传播。不访问真实数据目录、数据库或共享服务。隔离通过不代表实际部署已完成升级。

[三项目源码交叉测试](../../../../tests/test_registry_service_source_contract.py)串联本项目真实 `RegistryFileClient`、注册中心真实下载路由及认证客户端、中间件真实只读验证路由；只替换会话存储、SQL 与原件内容。覆盖无用户登录成功、非知识平台拒绝、调用平台 token 失效、员工等级不足、注册中心自身 token 失效五种情形，验证拒绝时不读取原件，以及两个平台令牌分别失效、不互相误伤。需要相邻源码及各自 Conda 环境，缺少时明确跳过。

2026-10-26 验证：11 项服务身份/缓存/默认助理工厂装配测试通过，1 项三项目交叉测试的五种情形全部通过；本项目全量 136 项测试中 125 项通过、11 项独立数据库测试跳过。实际助理工具定义保持只有文件 ID 与页码，新增引用和模块编译检查通过。未重启正在运行的服务，未执行真实文件下载或用户数据修改。

协作方验证：注册中心开发助手完成 18 项新增隔离测试及 10 项既有测试；本项目另独立运行中间件 `test_platform_token_verify.py`，9 项通过。上线按中间件、注册中心、知识后端的顺序加载配套代码；无需新增配置、用户登录或数据库迁移，已有文件权限继续生效。
