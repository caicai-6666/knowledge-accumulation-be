# 平台信息 API

面向已登录用户，实时读取中间件平台基础信息，并附加权威数据库中的管理员展示资料。没有平台或管理员写接口。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [查询平台列表](#查询平台列表) | `GET` | `/knowledge-accumulation/api/platform/list` |

---

## 查询平台列表

`GET /knowledge-accumulation/api/platform/list`，查询中间件返回的平台基础信息、能力与入口标记，以及各平台数据库管理员列表。

### 认证方式

唯一 `Authorization: Bearer <user-access-token>`，使用显式 login 返回的中央随机用户 token。复用普通中央 token 认证，先由中央查询取得可信 userid，再读取当前员工身份；不要求管理员身份或文件访问等级。开发固定登录凭据不能直接作为用户会话。

### 请求参数

无 path、query 参数，无 body。唯一必填 header 为用户 Authorization Bearer，格式遵循[认证 API](auth.md#通用约定)。任何 query 或非空 body 返回 422，不允许客户端指定平台代码、userid 或数据库。

### 成功响应

`200 application/json`，`Cache-Control: no-store`，返回数组：

```json
[
  {
    "code":"registry",
    "name":"知识沉淀注册中心",
    "description":"知识资料管理",
    "is_alive":true,
    "supports_a2a":false,
    "is_sub_management_platform":false,
    "is_management_entry":true,
    "admins":[
      {"name":"管理员甲","avatar_url":"0123456789abcdef0123456789abcdef.webp"},
      {"name":"管理员乙","avatar_url":null}
    ]
  },
  {
    "code":"knowledge-accumulation",
    "name":"知识沉淀",
    "description":"用户问答服务",
    "is_alive":false,
    "supports_a2a":false,
    "is_sub_management_platform":false,
    "is_management_entry":false,
    "admins":[]
  }
]
```

| 字段 | 类型 | 来源与语义 |
| --- | --- | --- |
| `code` | string | 中间件平台代码。 |
| `name` | string | 中间件平台名称。 |
| `description` | string | 中间件平台用途说明。 |
| `is_alive` | boolean | 中间件平台会话存活状态，不等同业务健康检查；离线平台仍返回。 |
| `supports_a2a` | boolean | 中间件配置声明的平台是否参与 A2A 协议；不代表 A2A 通信能力已就绪。 |
| `is_sub_management_platform` | boolean | 中间件配置声明的是否为子管理平台。 |
| `is_management_entry` | boolean | 中间件配置声明的是否作为管理平台入口，与子管理平台标记独立。 |
| `admins` | array | 权威数据库关联的管理员展示资料；没有管理员时为空数组。 |
| `admins[].name` | string | SQL 员工姓名，无姓名时回退 userid。 |
| `admins[].avatar_url` | string / null | 安全头像文件名或 null，不返回内部目录；头像读取见[资源 API](resource.md#读取头像)。 |

中间件返回空平台列表时，本接口返回 `[]`，无需查询管理员资料。最新中间件契约按配置顺序返回所有配置平台，本项目保持返回顺序和范围，不根据三个标记、在线状态或当前用户权限额外过滤；SQL 中其他平台的管理员关联不能将未返回平台补入列表。管理员展示不包含密码、token、权限等级或 managed_platforms。

三个新增标记始终输出真实 boolean，逐项透传中间件值，不从管理员列表或在线状态推断，也不新增本地默认值。缺失、null、数字或字符串布尔值均视为上游契约错误，返回 503。标记仅描述平台配置，不授予用户文件访问权限或管理权限；示例值不代表实际部署配置。

### 错误响应

错误为 `application/json`，使用 no-store，不回显原始请求、上游响应或数据库异常：

| 状态码 | detail 示例 | 触发条件 |
| --- | --- | --- |
| `401` | `用户 token 无效、过期或中央登录已离线` | Bearer 不合法、中央 token 失效/被新登录替换或当前员工不存在。 |
| `422` | `请求参数不合法` | 存在 query 或 body；不回显输入。 |
| `503` | `中间件接入未配置，用户认证暂不可用` | 本项目中间件接入未配置。 |
| `503` | `中间件用户认证暂不可用` | 中央用户认证依赖故障。 |
| `503` | `中间件平台会话暂不可用` | 无有效平台 token，不能查询目录。 |
| `503` | `平台信息暂不可用` | 平台目录、管理员 SQL、严格字段校验或总查询超时失败。 |

中间件目录的 401 视为平台凭据依赖故障，映射 503，仅失效本次使用的平台 token，不撤销用户 token，也不清除并发取得的新平台会话。数据库失败不能降级为管理员空列表，目录故障不能降级为空平台列表或使用旧存活状态。

### 请求示例

替换地址和 login 返回的随机用户 token：

```bash
curl 'http://127.0.0.1:24000/knowledge-accumulation/api/platform/list' \
  -H 'Authorization: Bearer replace-with-central-user-access-token'
```

### 行为与边界

[PlatformQueryService](../../app/service/platform_query.py)每次使用本项目平台 Bearer 调用 `GET /middleware-service/api/platform/list`，复用已有 MiddlewareClient 的严格响应校验及禁用环境代理/重定向策略；从中间件读取平台目录。随后通过现有 `list_platform_admin_profiles()` 一次 SQL JOIN 批量读取各平台管理员姓名与头像，按 code 合并，避免逐平台查询。

响应 DTO 为 [PlatformResponse](../../app/schema/platform.py)，继承公开基础字段 [PlatformInfo](../../app/model/platform.py)；三个布尔标记仍原样返回，管理员复用 PlatformAdminProfile。

上游使用同文件中的 `MiddlewarePlatformInfo` 独立接收新增必填字符串 `a2aresponsibility`（平台在 A2A 协作中的职责）。非子管理平台可以为空字符串；子管理平台必须为非空、非纯空白说明，即使 `supports_a2a=false` 也不例外。缺失、null、错误类型或非法空职责沿用契约故障处理；不默认补造职责，不改变授权。客户端及内部目录保留该字段，`PlatformQueryService` 显式选择原有公开字段，因此本项目 `GET /knowledge-accumulation/api/platform/list` 的 JSON、OpenAPI 和管理员来源均不变，不新增职责字段。

员工首次初始化仍只使用目录中的平台代码初始化零权限，不根据新增标记提升权限。SQL 管理员来自与 registry 共用的权威人员库；不读取模拟 managed_platforms 配置补展示，不把中央管理员缓存作为展示资料来源，也不从文件权限等级推断管理员。

列表是展示信息，不作为管理员鉴权依据。调用方需要管理授权时，仍应通过自身平台身份向中央查询管理员名单。中间件目录与 SQL 读取不是跨系统原子快照；本接口无后台缓存，不提供下次同步时间或历史存活时间。

组合根与认证/注册服务共享已有平台会话、HTTP 客户端和仓库，不新增后台任务或配置项。查询阶段总超时复用 `AUTH_OPERATION_TIMEOUT_SECONDS`（默认 60 秒），HTTP 与数据库同时受既有各自时限约束；资源仍由组合根统一关闭。本接口不持 maintenance_lock，不创建登录、不调用 user/update，不发布用户或管理员全表、不修改 SQL 或实际配置。

---

## 验证

新增[平台查询测试](../../tests/test_platform_query.py) 16 项，覆盖普通用户、空平台/空管理员、隐藏关联、离线平台、实时重读、token 失效、平台 401 与迟到 401、SQL/网络/契约故障、脱敏、无额外参数、超时/取消、不持维护锁及不发布全表。平台接口新增阶段完整回归 **165 项通过**，此前 149 项继续保留；移除注册开关后的当前结果见[注册验证记录](../capability/application/startup-user-sync.md#验证与结果)。

HTTP 与数据库均隔离 Mock，测试临时目录显式在 tests/tmp；未调用共享中间件、真实数据库或读取真实头像，也未修改实际 `.env`。

2026-10-23 新目录契约验证：平台查询、出站认证契约、员工初始化和认证模块共 64 项通过。覆盖三个标记的全部真假组合、与在线状态/SQL 管理员的独立性、OpenAPI 必填 boolean，以及缺字段或非法类型返回脱敏 503。共享认证模拟目录同步为新契约，员工初始权限仍为零；本次仅隔离验证，没有调用真实中间件。

2026-10-23 A2A职责字段适配：相关66项隔离测试通过，验证客户端保留 `a2aresponsibility`、非子管理平台空值可用、子管理平台非空约束、缺失/错误类型拒绝，以及对外JSON与OpenAPI字段集合不变。员工初始化和认证回归通过；未调用真实中间件或修改实际配置。
