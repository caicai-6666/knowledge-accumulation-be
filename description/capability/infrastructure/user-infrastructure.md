# 用户基础设施

## 用途与模块关系

本模块准备人员仓库、开发模拟身份、钉钉 HTTP 适配和头像安全存储，为后续逐项迁移用户业务提供基础。字段定义统一见[人员与权限数据契约](../../architecture/data/personnel.md)，总表发布规则见[启动用户总表同步](../application/startup-user-sync.md)。

新后端业务前缀固定为 `/knowledge-accumulation/api`；来源项目继续保留 `/knowledge-accumulation/registry/api`，本阶段不修改来源代码。显式登录及中央 token 身份查询已由[登录与会话管理](../../architecture/system/user-auth.md)提供；已提供复用普通中央 token 认证的[头像读取 API](../../api/resource.md)，现另提供[平台查询 API](../../api/platform.md)，平台基础信息取中间件、管理员展示资料批量取数据库。

---

## MySQL 异步仓库

[MySQLEmployeeRepository](../../../app/repository/employee.py) 使用 `asyncmy` 连接池和 `DictCursor`，不使用 `aiomysql`。接口包括：

- `list_departments()`：读取顶级部门。
- `find_by_userid()`、`list_users()`：读取员工及关联部门、文件权限和独立管理员身份。
- `list_platform_admins()`、`list_platform_admin_profiles()`：读取管理员关联或展示资料；权威关联供[启动管理员全表同步](../application/startup-admin-sync.md)使用，SQL 编辑与增量维护不迁移。
- `initialize_employee()`：供可信首次登录显式调用的事务入口，更新部门名称并插入缺失员工；已有员工不覆盖。
- `close()`：关闭连接池并等待连接释放。

连接池首次查询时惰性创建，SQL 参数绑定与查询语句分离，查询和连接建立有超时。启动同步只读取员工，不执行初始化写入。应用没有建表逻辑，部署侧必须提前准备正确的人员表及权限；数据库错误不能解释为空总表。

MySQL 8 的 `caching_sha2_password` / `sha256_password` 完整 RSA 认证还依赖显式安装的 `cryptography`，不能仅凭 Mock 仓库验证判断实库可用。依赖选型及本地 RSA 路径验证见[MySQL 8 认证依赖](../application/dependencies.md#mysql-8-认证依赖)，不得通过降级认证插件绕过缺包错误。

[MySQL 配置](../../../app/config/database.py) 从项目根目录 `.env` 读取，环境变量优先：

| 配置项 | 默认值 |
| --- | --- |
| `MYSQL_HOST` / `MYSQL_PORT` | `127.0.0.1` / `3306` |
| `MYSQL_DATABASE` / `MYSQL_USER` | `knowledge_accumulation` / `knowledge_accumulation` |
| `MYSQL_PASSWORD` | 必须提供非空值，使用 SecretStr 保存且排除序列化。 |
| `MYSQL_CONNECT_TIMEOUT_SECONDS` | `5` |
| `MYSQL_OPERATION_TIMEOUT_SECONDS` | `10` |

默认库名是隔离目标命名，不表示库已建立或已迁入人员数据，也不证明已与 registry 使用同一权威源。正式全表注册须与 registry 权限编辑共用权威人员表，不能维护两份分叉副本；跨后端覆盖限制见[职责与运维边界](../../architecture/system/user-auth.md#中间件维护方授权与运维状态)。不要将新项目直接指向真实业务数据库进行本阶段验证。

---

## 开发模拟身份与钉钉适配

[AuthSettings](../../../app/config/auth.py) 使用 `APP_ENV=development|production`，开发模式由 `simulated_user()` 构造服务端配置身份，生产模式返回 `None`。配置准备不等于模拟用户已经登录。开发 userid 已在 SQL 时，认证与同步均以 SQL 员工及管理员关系为准，配置只用于尚未落库身份；一次性固化规则见[开发账户落库](../../architecture/system/user-auth.md#开发账户落库)。用户 token 配置见[登录配置](../../architecture/system/user-auth.md#开关与配置)，资料同步本身不发送用户 token。

| 配置项 | 默认值与说明 |
| --- | --- |
| `AUTH_DEVELOPER_USERID` / `AUTH_DEVELOPER_NAME` | `developer` / `开发模拟用户` |
| `AUTH_DEVELOPER_DEPARTMENT_ID` / `AUTH_DEVELOPER_DEPARTMENT_NAME` | `0` / `开发模拟部门` |
| `AUTH_DEVELOPER_AVATAR_PATH` | 空；可提供合法内部头像引用，构造身份时转换为文件名。 |
| `AUTH_DEVELOPER_PERMISSIONS` | JSON 对象，默认 `{"registry":0}`；这里的 registry 是文件权限目标，不是调用方平台身份。 |
| `AUTH_DEVELOPER_MANAGED_PLATFORMS` | JSON 数组，默认 `[]`；与文件权限独立。 |
| `DINGTALK_CORP_ID` / `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` | 构造钉钉客户端时必填，纯员工表同步无需提供；本阶段不复制来源实际凭据。 |
| `DINGTALK_TIMEOUT_SECONDS` | `10` |

[DingTalkClient](../../../app/infrastructure/dingtalk.py) 提供免登 code 交换、员工详情及部门详情请求，缓存应用 token 并在应用凭证失效时刷新重试一次。验证员工和部门响应与请求身份一致，上游异常转换为不含原始响应的稳定错误。钉钉根部门 `dept_id=1` 的成功响应可省略 `parent_id`，在模型边界规范化为 `0`，供首次初始化解析；普通部门仍要求合法的父部门编号，不将缺失值猜测为根部门。HTTP 禁用环境代理与自动重定向，客户端由组合根关闭；独立注册协调器不使用它，生产应用仍为登录准备该客户端。

钉钉 CorpId、AppKey、AppSecret 的非空校验在客户端构造边界执行，任一缺失或仅含空白时抛出本地配置错误，且不创建 HTTP 客户端、不发送网络请求。配置模型可单独用于资料处理，但生产应用启动会在创建后台任务前拒绝缺失钉钉凭据；不再提供关闭登录的同步专用运行开关。开发应用启动也必须提供合法固定登录凭据。

---

## 头像下载与安全存储

[AvatarStore](../../../app/infrastructure/avatar.py) 校验 HTTPS 域名白名单、凭据和端口，每次重定向重新校验目标。按流限制下载大小，校验图片实际内容、累计帧像素及色彩模式，不信任 Content-Type。不缩放或量化图片，编码为无损 WebP，保留透明像素 RGB、动画帧及时间信息；无法无损表达的格式明确失败。

静态和动画帧均采用 `lossless=True, quality=100, method=4, exact=True`；压缩计算量使用适中档位，避免首次机器人建档被过高计算量拖至接收超时，像素保真要求不变。2026-10-26 对用户报告的首次建档超时进行只读复现：员工及部门查询分别约 0.38/0.12 秒，头像下载约 0.44 秒；1680×1682 JPEG 在原 `method=6` 下无损编码约 10.85 秒，超过机器人 10 秒回调预算。调整为 `method=4` 后约 1.54 秒，产物从 3141364 增至 3159998 字节，解码后 RGBA 像素逐字节相同；耗时仅代表本机该样本，不保证所有头像耗时相同。原始照片和编码产物只驻留内存，未写入 `data/`，未手工初始化员工。

[无损回归](../../../tests/test_avatar_lossless_encoding.py)3 项验证透明像素隐藏 RGB、拍摄方向和动画帧/时长/循环；机器人相关回归 38 项验证头像处理被取消时仍保留 `received`、不建档/发布，重推可继续。共 41 项通过，`git diff --check` 通过。首次初始化失败或取消的日志记录固定阶段、异常类型与耗时，不包含员工身份或头像链接；Stream 超时日志同时输出回调预算。

文件使用随机 32 位十六进制名称，经过临时文件、fsync 和原子替换写入。读取限定合法引用、普通文件，使用目录描述符及 O_NOFOLLOW 拒绝文件和头像根目录符号链接，拒绝超大内容。写入线程取消时等待完成并清理未使用产物。

| 配置项 | 默认值 |
| --- | --- |
| `AVATAR_ROOT` | 项目根目录下 `data/user/avatar`，仅真实运行使用。 |
| `AVATAR_MAX_DOWNLOAD_BYTES` | `10485760`，同时限制下载、编码后和读取的大小。 |
| `AVATAR_MAX_PIXELS` | `16000000`，动画累计计数。 |
| `AVATAR_TIMEOUT_SECONDS` | `10` |
| `AVATAR_ALLOWED_HOSTS` | `dingtalk.com,alicdn.com,aliyuncs.com`，允许相应子域名。 |

构造配置和 Store 不创建目录，GET 读取也不创建目录或写文件。配置中间件接入时，开发/生产均由组合根提供唯一 Store，生产首次初始化复用该 Store；中间件接入缺失时不创建它。接口、响应头和错误定义见[头像读取 API](../../api/resource.md)。测试显式覆盖 root 至 `tests/tmp/`；真实头像复制与配置引用更新必须另经用户授权，不能在测试中使用默认 data。

---

## 依赖与验证

除现有 `asyncmy`、`httpx`、`pydantic` 和 `python-dotenv` 外，新增 `Pillow==13.0.0`，已安装至项目 Conda 环境。完整版本见 [requirement.txt](../../../requirement.txt)。

[基础设施测试](../../../tests/test_user_infrastructure.py)使用异步连接池 Mock 和 HTTP Mock 验证字段映射、参数绑定、只读查询、事务回滚及钉钉响应；[头像存储测试](../../../tests/test_avatar.py)在 `tests/tmp/` 验证无损像素、动画、下载限制、符号链接、原子写入及取消清理。

### 头像读取接口验证

新增[头像资源测试](../../../tests/test_avatar_resource.py) 17 项，覆盖普通无权限/非管理员访问、开发随机 token、精确原始字节与响应头、缺文件、非法名称/编码路径/换行、符号链接、非普通文件、超限、脱敏、超时与取消、中央失效/异常、数据库故障、认证关闭及两种模式 Store 仅关闭一次。接口新增后完整回归 **126 项通过**（此前中央 token 阶段 109 项保留），所有 HTTP/人员查询隔离 Mock，测试文件仅在 `tests/tmp/`。

### 授权单文件迁移准备

头像接口完成后，按用户本次单独授权，仅复制 `dde93bcb74da4e7faaaf1337ba4ff09c.webp` 至本项目 `data/user/avatar/`。复制前后均核验普通文件、133108 字节及 SHA-256 `db1c49bfbe8499e5478206a00cd7a6e6bf8c0d9c2d4b84c3bb5402efc573eb7d`；目录和文件拒绝符号链接，目标使用独占创建，不覆盖不同内容。复制当时来源保留，本方未删除或更改来源；后续协作方验收及清理结果见下文。

本次实际 `.env` 唯一变更为 `AUTH_DEVELOPER_AVATAR_PATH=user/avatar/dde93bcb74da4e7faaaf1337ba4ff09c.webp`，其他配置和凭据逐项保持原值，两个开关继续 false。`.env.example` 中该项保持空值，真实头像目录由 `.gitignore` 排除。已本地校验模拟身份仍返回原文件名，没有向共享服务或真实 API 发请求；文件迁移准备不代表真实登录、读取链路或生产切换已完成。

### 2026-10-25 本机开发用户初始化

按用户授权，将本项目 `data/user/avatar/develop-avatar.png` 无损转换为 `5540567673344b97857694198aa51ca3.webp`，保留原 PNG；更新实际 `.env` 的 `AUTH_DEVELOPER_AVATAR_PATH` 为 `user/avatar/5540567673344b97857694198aa51ca3.webp`。新头像为 500 × 474 像素、133108 字节，SHA-256 为 `db1c49bfbe8499e5478206a00cd7a6e6bf8c0d9c2d4b84c3bb5402efc573eb7d`。此记录对应当前本机文件，前文旧头像路径为历史迁移记录。

写入前确认当前配置数据库不存在 `developer` 员工和 ID 为 0 的部门，再在单个事务中创建部门“综合管理部”、开发员工“奶蛙”以及配置中的 `contract-pheno`、`registry` 两项管理员关联；两个平台的文件权限等级均为 0。数据库头像保存上述内部引用，未改写其他员工。

验证通过：原 PNG 与 WebP 解码后的 RGBA 像素逐字节一致；实际人员仓库回读身份、部门、权限及管理员关联与配置一致；实际 AvatarStore 可读取相同 WebP 字节；重新加载配置获得新头像引用。未启动应用或执行中央用户/管理员总表发布，未验证真实登录链路。

用户随后将开发用户配置中的部门名称改为 `TVA`，已同步更新数据库 ID 为 0 的部门名称。更新前确认该部门仅关联 `developer`，更新后通过人员仓库回读确认名称为 `TVA`；上述“综合管理部”为初次写入时的名称。

### 协作方头像迁移最终验收

registry 主开发助手报告：已使用目标实际配置及实际 AvatarStore 读取目标 WebP 成功，确认 133108 字节、上述 SHA-256、开发头像引用与 `.gitignore` 均一致。随后由其删除 registry 原头像文件及空目录，并仅从 registry 实际 `.env` 移除 5 个 AVATAR_* 配置及 AUTH_DEVELOPER_AVATAR_PATH，其他配置逐项未变。本方不再读取或复制已清理来源，目标头像文件保持不动。

本记录依据协作方独立反馈；其另报 registry 完整回归 470 项（22 跳过）、374 个文档链接检查通过，与知识后端测试计数分开。本方最终仍为此前完整 **126 项通过**、pip check、111 个文档链接及 OpenAPI 检查通过。此次收尾仅更新本说明，未修改代码、配置或头像，未重复运行测试；目标 126 项由协作方后续只读复跑。本次验收不代表共享服务或生产登录已切换，两个默认开关保持关闭。

### 协作方隔离 MySQL 8.4 实库补验

根据 registry-be 主开发助手在此前职责阶段的复核反馈，已使用新启动的本项目 Conda 解释器，对独立临时容器 `knowledge-auth-mysql-check` 的 `knowledge_auth_test` 测试库完成补验：服务端 root 使用默认 `caching_sha2_password`，`asyncmy` 配合 `cryptography==50.0.2` 握手成功；只读引用既有 003/004 DDL 在临时库建表后，员工初始化、区分大小写的精确 userid、管理员关联、头像内部引用到文件名映射、已有员工重放不覆盖姓名/头像/权限，以及非法员工触发部门和员工事务回滚均通过。

协作方随后独立重跑本项目完整 87 项测试及 `pip check`，均通过。此记录作为历史底层能力验证保留，不代表最终分工后的注册/登录流程已通过实库或共享服务验收；无需重复等待该次补验。

以上为协作方报告的隔离实库及复核结果，本助手未重复执行实库操作，也未收到单独的原始日志文件。协作方确认临时容器已清理，未修改项目文件、实际 `.env` 或真实数据库。该结果不代表生产钉钉、共享中间件或生产数据库已联通，也不代表人员数据迁移、真实授权或切换完成；登录和全表同步仍默认关闭。

### 移除登录开关后的当前验证

用户要求移除 AUTH_ENABLED 后，实际 `.env` 和示例已删除该项，其他配置值与真实头像保持不变。登录随本项目中间件接入直接装配，开发固定凭据或生产钉钉凭据在启动边界严格校验；无中间件接入时认证返回依赖 503。全表注册开关继续默认 false，中央 token、权限职责、首次登记及头像读取契约不变。

完整回归 **128 项通过**，覆盖无开关登录、缺失开发/生产凭据在后台网络任务前失败，并保留注册与中央 token、头像安全测试。上述 126 项为本次配置简化前的历史阶段结果，原关闭登录测试已更新为依赖缺失/凭据校验测试；未启动应用连接共享服务，未重读真实头像或来源。

管理员启动全表同步后当前完整回归 **149 项通过**，原 128 项保留；新增职责与独立默认关闭边界见[管理员同步专题](../application/startup-admin-sync.md)。

### 移除全表同步开关后的当前验证

用户确认中间件完成适配后，MIDDLEWARE_USER_SYNC_ENABLED 已从代码、实际 `.env` 和示例移除。中间件接入已配置时，启动自动同步用户表、随后同步管理员表，显式登录遇到确切资料缺失可受控补登记；不新增替代开关，普通查询不写表。

当前完整回归 **166 项通过**，包括旧 false 环境配置不能关闭同步的校验。前述“默认关闭”及 128/149/165 项是各迁移阶段历史记录，当前行为以本节与[注册协调专题](../application/startup-user-sync.md)为准。本次没有主动启动真实服务，未修改其他实际配置值或头像文件；运行时只有中央成功确认两阶段响应后才算完成登记，不将隔离测试当成真实中央名单已更新。


---

## 任职资料初始化扩展

新增 `employees.work_profile` JSON 存储和内部读取入口，首次初始化采集钉钉直属主管 userid、职位名称。岗位说明书已按用户要求移出范围。字段契约、手工迁移、权限、失败边界及历史用户策略统一见[任职资料与初始化](../../architecture/data/personnel.md#任职资料与初始化)。本轮未执行真实数据库迁移或钉钉联调。

[任职资料隔离测试](../../../tests/test_employee_work_profile.py)共 6 项通过，覆盖 HTTP 字段解析、初始化传递、未知值、已有员工不覆盖、上游失败及字段校验；另用 Mock 连接验证 JSON 参数绑定、事务提交/回滚和内部回读。`git diff --check` 通过。测试不连接真实外部服务，不写入应用数据目录；迁移 SQL 尚未进行实库验证。测试文件依项目白名单规则仅保存在本地工作区。
