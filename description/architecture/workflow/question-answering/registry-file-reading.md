# 注册中心文件图像查看

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

## 原件获取与用户凭据

[RegistryFileClient](../../../../app/infrastructure/registry_file.py)只调用固定接口：

```text
GET {REGISTRY_BASE_URL}/knowledge-accumulation/registry/api/resource/authorized-file/{resource_id}
Authorization: Bearer <当前用户access_token>
```

不传query/body，不发送平台token或registry平台凭据。缓存未命中时使用此接口下载，registry仍独立验证中央用户会话及文件来源平台等级。缓存命中时改由本平台实时校验中央身份和权威SQL文件权限，不请求原件。不要求管理员身份；客户端不创建、恢复或更新登录状态，不自动重试登录，不跟随重定向，不读取环境代理。

探索任务首次SSE订阅已经通过中央身份校验，路由将同一Bearer包装为`SecretStr`，仅传给本轮生产者及图像查看器；注册任务时不保存token。token不写入`_Turn`、任务输入、用户身份模型、SQL、工作区、模型上下文、工具回执或SSE事件。任务正常结束、失败、停止或被替代后释放查看器持有的token引用。SSE重连仍执行原有中央校验，但不会替换已经启动任务的凭据或再次激活任务；原token被新登录替换时，后续文件读取返回失效提示，需要新任务使用新的有效token。

内部Python宿主通过`QuestionAgentService.generate(..., access_token=SecretStr(...))`提供与可信用户身份对应的当前凭据。仅有userid或历史身份快照不够，缺失token时本地失败、不发送HTTP。助理与探索工厂支持 `registry_files` 和 `registry_file_access` 依赖。组合根注入共享下载客户端及 `RegistryFileAccessService`，后者复用中央会话服务和文件检索服务的权威只读查询；不另建连接或缓存身份。停机先结束问答任务再关闭依赖。

---

## PDF、安全读取与缓存

[RegistryFileViewer](../../../../app/agent/core/file/registry_viewer.py)复用[图像查看器](image-reading.md)的页码游标、`FileModel`、视觉预算和`FileModelCache`。应用已装配本平台鉴权时，以 `(用户、树、正式资源ID)` 定位内存模型，不为缓存命中下载PDF或计算内容哈希；上传UUID与正式整数ID不混淆。查看器按任务隔离，重开任务从第一页开始；LRU淘汰后才重新下载，本轮游标保留。

每次工具查看、翻页和驻留图片再次注入都执行以下流程：

1. [RegistryFileAccessService](../../../../app/service/registry_file_access.py) 使用本轮用户token查询中央登录状态，返回userid必须与任务所有者完全一致；不使用本地token映射，也不创建登录。
2. 通过文件检索服务的 `read(userid, (resource_id,))` 直读权威SQL：同一只读快照中读取 `employees.platform_permissions` 和正式 `resources`。只比较文件实际来源平台对应的等级，缺平台键、未知文件等级、员工缺失或资源删除均拒绝。管理员列表和开发模拟配置不赋予文件权限，检索候选及向量缓存不参与判断。
3. 权限通过后取得LRU租约；命中直接渲染，未命中才带同一用户Bearer下载PDF并加载。渲染完成再次鉴权，权限变更或依赖故障不发布页面。鉴权失败不能回退为跳过校验的缓存读取或下载。

SQL配置沿用 `FILE_SOURCE_MYSQL_*`，必须指向registry实际维护的权威人员及资源表；不能接独立的旧副本，也不能只依赖当前任务身份中的权限快照。此机制仍需中央HTTP身份校验，但不再为命中缓存请求registry原件。无需新增中间件或registry接口。

正式资源ID对应已入库的不可变原件，重新入库使用新ID；文件等级变化继续实时查SQL。直接在磁盘原地改写、删除原件而不更新资源登记不属于支持的更新方式，缓存命中不会检测这种带外操作。内部宿主若未装配本平台权威鉴权，保留每次GET授权及内容摘要校验路径，绝不能无条件复用；已装配鉴权但查询失败时不会退回该路径。

原件仅在内存处理，不写入`data/user/upload-file`，不复制注册中心磁盘文件。响应只接受200、`application/pdf`或`application/octet-stream`和identity编码；下载前检查声明长度、流式累计检查实际字节、核对完整长度及PDF签名，再由MuPDF校验真实PDF、页数和打开密码。不信任文件名或MIME来代替内容验收。

阅读位置和可解析引用只在工具结果提交成功后生效；失败不推进。引用与具体查看器、资源ID及页码绑定，伪造或跨任务引用不可读。关闭查看器清理游标、页面引用和token；关闭公共池时等待渲染租约归还，不强制释放正在使用的MuPDF对象。图像自动折叠沿用`AGENT_PAGE_DISPLAY_ROUNDS`，折叠后不继续读取原件。

---

## 失败边界

| 情况 | 工具反馈 |
| --- | --- |
| 无本轮token | `user_session_required`，不发送HTTP。 |
| registry返回401 | `user_session_expired`，不恢复登录。 |
| registry返回403，或本地SQL不允许读取 | `file_forbidden`，不使用已缓存图像绕过授权。 |
| registry返回404 | `file_not_found`；本地SQL查询不到可读资源统一返回 `file_forbidden`，不区分不存在与无权。 |
| 中央确认token失效或与任务用户不一致 | `user_session_expired` 或 `file_forbidden`，拒绝缓存且不恢复登录。 |
| 本平台中央/SQL查询故障或结果契约错误 | `file_authorization_unavailable`，不回退旧权限或重新下载。 |
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
| `REGISTRY_FILE_TIMEOUT_SECONDS` | 60 | 单次GET整体超时，同时用于HTTP请求超时。 |
| `REGISTRY_FILE_MAX_DOWNLOAD_BYTES` | 52428800 | 原件下载最大字节数，可按部署资源调整。 |
| `AGENT_FILE_CACHE_MAX_FILES` | 沿用现有值 | 与上传附件共用PDF模型LRU容量。 |
| `AGENT_PAGE_DISPLAY_ROUNDS` | 沿用现有值 | 原生图片的完整响应展示轮次。 |

示例见[.env.example](../../../../.env.example)。无需新增数据库表或依赖，不改文件查询结果集契约；本轮不修改真实.env、registry、部署或前端。

[隔离测试](../../../../tests/test_registry_file.py)使用MockTransport和内存合成PDF，验证精确Bearer转发、HTTP失败、超时/取消、下载上限、页码、原生PNG、权限复核、LRU、引用、折叠、实际Core调用、SSE首次激活凭据传递及停机顺序。旧上传附件测试同时回归；隔离通过不代表真实registry已联通。


[缓存权限隔离测试](../../../../tests/test_registry_file_access.py)验证首次下载后连续展示/翻页不再GET、缓存命中仍查询中央token与权威权限、token失效/身份不符/权限撤销/资源不可读时拒绝、SQL或中央故障不降级、淘汰后重载、跨用户隔离及渲染期间撤权。结合实际Core与模式工厂测试验证依赖透传；全部使用MockTransport、内存PDF和隔离替身，不访问真实数据库或共享服务。
