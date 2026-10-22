# 登录与会话管理

## 范围与职责

本认证模块提供新用户中心的两个入口：[用户认证 API](../../api/auth.md)。本阶段提供登录和当前身份；原下游 verify 路由已移除，下游直接向中间件查询用户 token，不再依赖知识后端验证。头像读取使用同一普通中央 token 校验，见[用户资源 API](../../api/resource.md)。平台列表也复用普通认证，见[平台信息 API](../../api/platform.md)。当前不迁移管理员鉴权、注册中心资源或审核业务。

用户会话由中间件中央用户 token 唯一决定；知识后端不保存 token→userid 映射，也不设置本地固定 TTL。平台会话继续使用已有 `knowledge-accumulation` 身份，其 Bearer 只认证平台；用户 access_token 放在中央请求 body，二者不能混用。

---

## 显式登录与只验证

| 入口 | 身份确认与中央操作 | 结果 |
| --- | --- | --- |
| 生产 `POST auth/login` | 钉钉 code 确认 userid，读取或初始化员工，生成新随机 token，PUT 绑定。 | 中央确认成功后返回 token、身份及中央 ttl。 |
| 开发 `POST auth/login` | 本地校验固定 Bearer 凭据且禁止 body/query，优先读取配置 userid 对应的 SQL 员工，生成新随机 token，PUT 绑定。 | 返回新的用户 token；固定凭据不作为用户会话。 |
| 任意模式 `GET auth/me` 及普通业务依赖 | 仅将用户 access_token 发送中央 POST 查询，取得可信 userid 后读取身份。 | 无效/过期为 401；不创建或恢复登录。 |

显式登录生成 32 字节随机不透明 token；普通资料已存在时只 PUT，不先更新资料、不因本地没有登记记录而重发全表。PUT 精确 body 为 `{"userid":"可信userid","access_token":"新随机token"}`，平台 Bearer 在头部。只有绑定成功、响应身份及状态校验通过后才向客户端返回 token；失败无 token 响应，不把请求结果不确定解释成成功。

若首次 PUT 按既有契约明确返回 `404 {"detail":"用户不存在，请先同步用户信息"}`，由同一维护锁内的全表协调器重读完整权威快照、完成注册后，使用本次同一随机 token 最多重试一次 PUT。普通路由 404、401、403、409、网络或响应异常不能触发全表；登记失败时返回 503。第二次 PUT 仍失败即结束，不循环注册。注册规则见[用户全表注册协调](../../capability/application/startup-user-sync.md)。

知识后端不调用 `POST user/update`，不修改既有权限或 SQL 管理员关系；本阶段新增的管理员启动全表镜像见[管理员同步](../../capability/application/startup-admin-sync.md)。首次基础身份、钉钉部门/头像及新员工初始 0 权限仍由本项目初始化，不属于 registry 的既有员工权限编辑。仅验证路径不更新资料、不注册、不 PUT 登录。

---

## 首次员工初始化

[EmployeeOnboardingService](../../../app/service/employee_onboarding.py) 在可信钉钉 userid 首次登录时执行：

1. 先读取已有员工；存在则直接使用，不重置任何姓名、头像、部门、文件权限或管理员身份。
2. 获取与可信 userid 相符的钉钉员工详情，向上追溯部门层级到根部门或根下一级部门。检测环、深度超过 32、断裂及多个不同顶级部门，不能确定唯一归属时失败。
3. 通过轻量 [PlatformDirectoryService](../../../app/service/platform_directory.py) 实时读取中间件平台目录。包含返回列表中的离线平台，并补入自身 `knowledge-accumulation`，为每个平台初始化 0 文件访问等级，不赋予管理身份。
4. 有头像时调用既有安全下载与无损 WebP 存储，再通过人员事务写入部门及缺失员工。没有头像使用 null。
5. 并发初始化由进程内 userid 锁和数据库主键共同约束；已有员工不会被后来的尝试重置。失败时清理未引用头像；数据库提交结果不确定且无法确认引用时保留头像供核对，不冒险删除可能已生效的文件。

**目录可见性边界：** 当前中间件 `GET platform/list` 仅返回 `show_in_list=true` 的平台。新员工初始化覆盖全部返回平台（含离线）与自身，不虚构其他隐藏平台键。缺少平台键表示未授权，不能当成默认 0 或自动允许访问，具体语义以[人员权限契约](../data/personnel.md#部门员工与管理员)为准。已有员工再次登录不重置或补全权限，目录可见性改变也不会自动扩权。完整隐藏平台目录须由用户另行协调外部契约，本阶段不新增目录接口或门户路由。

人员字段及权限定义见[人员与权限数据契约](../data/personnel.md)，头像和仓库实现见[用户基础设施](../../capability/infrastructure/user-infrastructure.md)。

---

## 开发账户落库

`AUTH_DEVELOPER_USERID` 是固定开发登录凭据绑定的服务端身份。该 userid 已在 `employees` 中时，login/me、用户全表和管理员全表均使用同一 SQL 权威源；配置的姓名、头像、部门、权限及 managed_platforms 不再覆盖该员工。数据库故障返回 503，不能降级为模拟资料。固定凭据仍只用于显式登录换取中央随机 token。

开发账户按用户授权一次性初始化部门、员工和初始管理关系；应用启动和普通请求不自动 INSERT 或重置。后续权限及管理关系修改仍归 registry。仅在没有对应 SQL 员工时保留原配置模拟路径；生产模式始终不合并模拟资料。两阶段发布之间身份从模拟变成 SQL 或反向变化会拒绝管理员发布，需核对数据源并重新启动，不用混合快照扩权。

---

## 本地与中央会话规则

[随机 token 生成器](../../../app/auth/session.py) 不存会话。[MiddlewareUserAuthService](../../../app/service/middleware_user_auth.py) 的查询只发送 `POST login-cache`，body 精确为 `{"access_token":"用户token"}`，绝不传 userid。在线响应必须同时满足 userid 非空合法、is_logged_in=true、ttl 为正严格整数；离线必须为 userid=null、false、0。PUT 还必须返回与可信身份相同的 userid 和在线状态，禁止类型宽松转换或额外字段。

中央有效查询负责续期；`expires_in` 仅表示登录响应时中央确认的 ttl，不是知识后端固定截止时间。知识后端重启和关闭不撤销中央 token；其他进程已绑定的有效 token 也能查询，身份不依赖本进程是否签发过。中间件同用户新登录使旧 token 立即失效；me 不能通过重新查库或钉钉信息复活它。

用户 token 无效/过期及员工不存在返回 401。数据库、网络、平台会话或非法响应返回 503，不向中央删除用户会话；依赖恢复可用同一有效 token 重试。中央 HTTP 401 属于平台凭据错误，仅失效本次使用的平台 token，映射 503；迟到旧 401 不清除新平台 token。

开发固定 token 仅用于 POST login 的本地凭据校验，错误凭据不查库、不发 HTTP。GET me 不将固定凭据直接认定为身份，而是像其他用户 token 一样只查中央；需要再次登录必须显式 POST login 换取新的随机 token。模拟管理列表可用于显示，且仅在开发 userid 尚未落库时由自动启动的同步任务合并发布管理员全表；login/me 自身不发布管理员表，管理鉴权仍取中央名单。

---

## 全表同步与登录并发协调

已配置中间件接入时，[组合根](../../../app/main.py)创建唯一进程内 `asyncio.Lock`，强制注入总表同步和中央认证服务。两个类不自行创建维护锁，避免分别加锁却无法消除跨服务竞态。

| 入口 | 同一维护锁覆盖范围 |
| --- | --- |
| 启动全表同步 | 完整员工快照 → 用户 full-table → 权威管理员关联及无冲突模拟合并 → 管理员 full-table。 |
| 生产显式登录 | 生成随机 token → PUT；明确资料缺失时完整快照注册 → 一次 PUT 重试。 |
| 开发显式 login | 读取开发 userid 的 SQL 身份（不存在才使用模拟资料） → 生成新 token → 同上绑定流程。 |
| me 及普通用户 token 查询 | 不获取维护写锁，仅查询并续期，不创建登录。 |

当同步先读到旧快照，显式登录必须等待该快照发布完成，再按需登记并绑定中央登录；反向顺序下，同步必须等待登录确认完成后才读取新快照。禁止在锁外提前读取快照，再仅对 full-table HTTP 调用加锁。

锁顺序统一为：外层操作总超时 → 维护写锁 → 读取当前平台 token → 数据库/中央操作。生产员工初始化的 userid 锁在进入中央写入前已释放，不与维护锁嵌套；开发显式登录不再另取用户写锁。锁等待计入现有超时，排队后重新取得当前平台凭据；取消或异常自动释放已持有的锁，不释放其他任务持有的锁。

该协调只覆盖本进程内的两个服务，仍要求单实例、单 worker。全表只有知识后端一个发布方；registry 仍是增量维护方，因此“唯一全表发布方”不能保证跨后端权限更新的一致性，外部覆盖风险见下文。调用超时后的上游提交不确定性仍按原故障规则处理，不假定远端操作已回滚。

---

## 开关与配置

配置示例维护在 [.env.example](../../../.env.example)。用户已要求移除 AUTH_ENABLED，实际 `.env` 同步删除此项，其他配置值保持不变。登录是核心能力，不再有独立启停开关。

| 配置项 | 默认值 | 约束与用途 |
| --- | --- | --- |
| `APP_ENV` | `development` | `development` 或 `production`。 |
| `AUTH_DEVELOPER_ACCESS_TOKEN` | 空 | 开发应用启动时必填，非空、无空白 ASCII；仅用于换取中央 token。 |
| `AUTH_OPERATION_TIMEOUT_SECONDS` | `60` | 一次登录/验证操作总超时，有限正数。 |
| `AUTH_MIDDLEWARE_TIMEOUT_SECONDS` | `5` | 一组中央操作或平台目录请求超时，有限正数。 |
| `DINGTALK_CORP_ID` / `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` | 空 | 生产应用启动时三项必填，在创建客户端或发送请求前本地校验。 |

模拟身份、MySQL 和头像配置沿用[基础设施配置](../../capability/infrastructure/user-infrastructure.md)。已配置中间件接入时直接装配登录：development 启动必须有合法固定登录凭据，production 启动必须有三项钉钉凭据；这些校验在创建后台 HTTP 任务前执行。生产不合并模拟用户，不再支持通过关闭登录来运行纯同步应用。MIDDLEWARE_USER_SYNC_ENABLED 已移除，启动自动注册用户和管理员全表，首次登录缺资料可受控补用户登记；不增加替代开关。

登录依赖中间件地址与本项目平台密钥；启用总表注册也必须先配置接入，平台代码必须为 `knowledge-accumulation`。中间件未配置时不装配认证依赖，请求返回依赖 503，不提供绕过中央状态的本地放行。

---

## 组合根与日志

[create_app()](../../../app/main.py) 只维护一个平台会话任务，按需共享员工仓库及用户 HTTP 客户端。开发模式不构造钉钉客户端；两种模式共享本应用唯一头像 Store，生产模式另构造钉钉、目录和首次初始化。登录和验证按请求执行，没有用户心跳后台任务。

关闭时先取消并等待可选全表同步，再按资源逆序关闭钉钉客户端、头像 Store、用户 HTTP 客户端、人员仓库及平台会话。依赖初始化失败也通过 `AsyncExitStack` 清理已登记资源。原平台单实例、单 worker 约束继续适用。

所有 HTTP 客户端禁用环境代理与自动重定向。异常日志只记录阶段、异常类型和状态码，不记录 code、token、密钥或原始响应。钉钉旧接口需在 URL 参数携带应用 token，因此应用关闭 `httpx`、`httpcore` 和 `uvicorn.access` 的明细日志；生产观测也不能另行开启会暴露请求头、URL 参数或请求体的日志。

请求 422 统一脱敏，不返回原始字段、输入或 Pydantic 错误上下文。认证响应包含 `Cache-Control: no-store`；不再暴露 verify 路由。

---

## 中间件维护方授权与运维状态

最终职责以用户明确划分为准，不再等待 `POST user/update` 对知识后端授权，也不依赖此前讨论的中间件配置变量。

| 中间件操作 | 责任方 |
| --- | --- |
| `POST user/full-table` | knowledge-accumulation，完整用户注册。 |
| `PUT user/login-cache` | knowledge-accumulation，可信显式登录通知。 |
| `POST user/update` | registry，用户资料与权限增量维护；知识后端不调用。 |
| `POST platform/admins/full-table` | knowledge-accumulation，启动自动全量镜像；用户已确认中间件完成适配。 |
| `platform/admins/add/remove` | registry，管理员增量维护；SQL 管理关系编辑也不迁移。 |
| `POST user/login-cache` | 双方按外部授权读取、续期已有登录。 |

用户已反馈 PUT 登录创建仅允许本平台，但本方未向共享服务验证整条链路。用户已确认中间件完成适配，完整人员范围与一致性保障仍须部署侧核对；不得借 registry 凭据绕过。权限和管理员读取、me 输出保留，不提供相应维护接口。

### 权威权限数据源与跨后端覆盖边界

全表必须从 registry 编辑权限所使用的**同一权威人员库/表**读取 `employees.platform_permissions` 与 `platform_admins`；registry 的增量维护必须先持久化到该权威源，不能仅更新中央 Redis。本项目不另建权限副本、不缓存权限后回写、不为旧员工补键或重新计算等级，仅在受控启动阶段镜像权威管理员关系，不修改源关系。开发 userid 已落库时只读 SQL，配置不覆盖姓名、权限或管理员关系；未落库时才添加独立模拟身份。

当前知识后端示例库名为 `knowledge_accumulation`，来源项目示例库名为 `registry`；示例不同不是共享权威源的证据。本轮未读取实际配置、未确认生产库指向及 registry 权限持久化顺序，必须由用户核对后配置，不能直接迁移出两份可独立编辑的人员副本。

即便共享权威源，现有 `users/count` 全表契约仍存在覆盖窗口：知识后端读到权限 9 → registry 持久化并增量撤销为 0 → 旧全表最后到达中央，仍可能将权限恢复为 9。组合根进程内锁不覆盖 registry，也不能保证其他进程或 worker 的一致性；仅在服务端接收请求后读取 Redis 版本、再重试同一个旧 payload，同样不能证明客户端快照新鲜。

该问题依赖外部契约：需用户协调可校验源快照版本的条件发布/过期拒绝，或覆盖“读取源快照至中央提交”的跨后端串行规则。本轮不新增外部接口或分布式设施，不宣称已解决。移除同步开关并不自动解决该边界，部署必须保证一致性；不能以每次登录重发全表规避增量维护职责。

本方不修改其他项目、部署或共享服务；本次仅从本项目实际 `.env` 删除过时同步开关键并同步注释，不改其他值；registry 旧全表任务由用户另行安排停用。知识后端仅保留本地实现与隔离测试，不执行真实切换或来源清理。

---

## 验证与切换前提

此前 **87 项回归、pip check 及协作方独立临时 MySQL 8.4 补验通过**作为历史验证保留，覆盖当时流程及底层仓库/RSA 能力；旧 `update → PUT` 流程的通过不等于新职责下登录闭环已验收。实库来源与范围见[隔离补验记录](../../capability/infrastructure/user-infrastructure.md#协作方隔离-mysql-84-实库补验)。

头像接口迁入前，中央 token 契约回归共 **109 项通过**。[认证测试](../../../tests/test_auth.py)覆盖新员工初始化/注册、同用户新登录轮换、精确 PUT/POST body、开发凭据换 token、me 不复活、无需本地映射、依赖恢复、脱敏、路由移除及清理。[出站契约测试](../../../tests/test_auth_contract.py)、[初始化测试](../../../tests/test_employee_onboarding.py)、[维护锁测试](../../../tests/test_user_maintenance.py)与[注册测试](../../../tests/test_user_registration.py)保留字段严格校验、默认关闭、首次注册、权限保留、共享锁交错、取消与超时覆盖。

跨后端交错测试刻画仍存在的旧权限覆盖风险，不是风险消除的证明。本轮使用 Mock 与显式隔离临时目录，未连接真实钉钉、数据库或共享中间件。历史 98/102 项是职责调整过程中的旧契约快照，不代表中央 token 方案验证结果；最终计数以本节完整回归为准。

### 协作方跨项目 token 隔离补验

registry 主开发助手报告：其可选 `tests/test_shared_login_token.py` 独立补验 1 项通过。测试在子进程使用知识后端实际 `MiddlewareUserAuthService` 与 `MiddlewareClient` 两次签发并绑定 token，确认 PUT 精确包含 userid/access_token、返回同一 token 及中央 ttl=77；随后 registry 实际查询客户端接受最新 token、拒绝旧 token，重建 registry AuthService 后新 token 仍可验证。

该补验全程使用隔离 MockTransport，没有真实数据库或中间件调用，也未修改知识后端文件。本方依据协作反馈记录，未重复执行，未收到独立原始产物。协作方另报 registry 完整回归 469 项（22 跳过）及 pip check 通过；这些计数与上述额外 1 项均不计入知识后端的 109 项。本方此前本地完整回归及 pip check 已通过，此次仅补充文档，不重复运行代码测试。

### 生产切换边界

上线仍需用户对齐真实授权、同一权威权限源、完整范围及跨后端覆盖协议，并安排停用旧全表发布方。中央 token 迁移阶段未修改其他项目、实际配置或共享服务；后续头像单文件迁移及唯一配置变更见[授权迁移记录](../../capability/infrastructure/user-infrastructure.md#授权单文件迁移准备)。后续已按用户要求移除登录和全表注册开关，启动自动同步；本助手未启动真实服务执行发布；当前回归见[移除登录开关后的验证](../../capability/infrastructure/user-infrastructure.md#移除登录开关后的当前验证)。

管理员启动全表迁入后的完整回归为 **149 项通过**，包含新增 21 项；当前数据流及授权边界统一见[启动管理员同步](../../capability/application/startup-admin-sync.md)。

注册开关移除后的当前完整回归 **166 项通过**，行为详见[自动全表注册](../../capability/application/startup-user-sync.md#验证与结果)。


### 2026-10-22 开发账户落库与授权启动验证

用户授权本项目实际建表、重启并全量同步后，已在当前 `registry` 权威库一次性固化开发员工、部门及初始管理关系；未修改实际 `.env`。开发身份现按本章 SQL 优先规则读取，已有人员记录未改写。新服务启动用户全表 count=7、管理员全表 admin_count=6；真实开发登录、me、探索列表及平台列表均 200。完整隔离回归 736 项（597 通过、139 环境条件跳过），pip check 通过。步骤、脱敏证据、旧会话 409 重试及范围限制见[部署记录](../../../experiment/deployment/20261022-exploration-init/report.md)。本节是后续授权执行结果；前文未真实发布的描述对应历史迁移阶段，不代表当前运行状态。
