# 用户认证 API

知识后端提供显式登录和中央用户 token 身份查询。平台 Bearer 与用户 access_token 不可混用；其他平台直接向中间件查询用户 token，不再调用知识后端 verify。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [显式登录](#显式登录) | `POST` | `/knowledge-accumulation/api/auth/login` |
| [查询当前身份](#查询当前身份) | `GET` | `/knowledge-accumulation/api/auth/me` |

## 通用约定

以下请求示例使用 Compose 端口 `24000`；本地 `python -m app.main` / IDE 使用 `15000` 且增加 `/dev` 前缀，见[API 地址约定](readme.md)。生产地址由部署提供。响应媒体类型 `application/json`，成功与错误均含 `Cache-Control: no-store`。登录无需独立开关；中间件接入未配置时返回 `503 {"detail":"中间件接入未配置，用户认证暂不可用"}`。请求不合法统一 `422 {"detail":"请求参数不合法"}`，不回显 code、token 或非法字段名。

Bearer 格式为唯一 `Authorization: Bearer <credential>`，scheme 大小写不敏感，凭据为 1 至 4096 个无空白可打印 ASCII 字符；重复头、错误 scheme 或额外空白返回 401。生产 code 登录不要求用户 Bearer；开发 login 使用固定登录凭据，me 使用换取的随机用户 token。

公共无额外输入校验读取请求体时，如收到客户端断线事件，则以非标准 `499` 终止该请求，不继续进入后续认证逻辑，不记录未处理的 `ClientDisconnect` 服务端异常。连接通常已经关闭，客户端不保证收到此响应；它不表示 token 失效，也不会撤销中央会话。正常空请求、非法 body/query 的校验保持不变；此处理只覆盖公共校验中的请求体读取，不吞掉任务取消或其他程序异常。

`POST /auth/verify` 已移除（404），无替代的跨平台知识后端验证接口。其他平台应使用自身平台 Bearer 调用中间件 `POST /middleware-service/api/user/login-cache`，body 仅含用户 `access_token`，由中央返回 userid/null 并续期。

---

## 显式登录

`POST /knowledge-accumulation/api/auth/login`，生产确认钉钉身份、开发确认固定登录凭据，均换取新随机中央用户 token。

### 认证方式

生产无需已有用户 Bearer，使用一次性钉钉 code。开发必须携带配置的固定 Bearer 登录凭据；它仅用于此次交换，不能作为固定用户会话。

### 请求参数

无 path、query 参数。根据服务端 `APP_ENV` 选择请求形式，客户端不能指定模式或 userid：

| 模式 | header | body |
| --- | --- | --- |
| production | `Content-Type: application/json` | 必填 `{"code":"钉钉一次性code"}`，禁止额外字段。code 为非空白字符串，长度 1–2048。 |
| development | `Authorization: Bearer <配置固定凭据>` | 无 body；`{}`、`null` 等非空请求体也拒绝。 |

OpenAPI 的 body/Bearer 均为条件可选，实际按上述模式严格校验。

### 成功响应

`200 application/json`：

```json
{
  "access_token":"已中央绑定的新随机用户token",
  "token_type":"Bearer",
  "expires_in":3600,
  "user":{
    "userid":"employee-a",
    "name":"员工甲",
    "employment_status":"employed",
    "department_id":2,
    "department_name":"财务部",
    "avatar_url":null,
    "platform_permissions":{"knowledge-accumulation":0,"registry":0},
    "managed_platforms":[]
  }
}
```

`expires_in` 是本次绑定成功时中央确认的剩余秒数，示例 3600 不代表固定配置。有效查询由中央续期，无本地固定 TTL。`user` 遵循[人员数据契约](../architecture/data/personnel.md#部门员工与管理员)。

### 错误响应

| 状态码 | detail 示例 | 触发条件 |
| --- | --- | --- |
| `401` | `钉钉免登 code 无效或已过期` / `开发登录凭据无效` | 可信身份验证失败，或开发 Bearer 缺失/错误。 |
| `422` | `请求参数不合法` | 模式对应的 body、code、query 不合法。 |
| `503` | `中间件接入未配置，用户认证暂不可用` | 未配置本项目中间件地址和平台凭据。 |
| `503` | `中间件尚未授权本平台执行全表注册或创建登录` | 中央返回 403。 |
| `503` | `中央用户资料尚未完成注册，不能确认登录` | 登记后仍缺失资料。 |
| `503` | `用户认证暂不可用` | 钉钉、数据库、头像、平台会话、登记、中央绑定或响应校验失败；具体 detail 为脱敏说明。 |

认证 401 含 `WWW-Authenticate: Bearer`；失败响应不返回 token。中央平台 401、token 冲突 409 映射为依赖 503，不冒充用户登录成功，也不改用其他平台凭据。

### 请求示例

生产替换真实一次性 code：

```bash
curl -X POST 'http://127.0.0.1:24000/knowledge-accumulation/api/auth/login' \
  -H 'Content-Type: application/json' \
  -d '{"code":"replace-with-fresh-dingtalk-code"}'
```

开发不传 body，替换固定登录凭据：

```bash
curl -X POST 'http://127.0.0.1:24000/knowledge-accumulation/api/auth/login' \
  -H 'Authorization: Bearer replace-with-developer-login-credential'
```

### 行为与边界

生产钉钉确认 userid 后读取或首次初始化员工；开发先在本地校验固定凭据，再读取服务端配置 userid 对应的 SQL 员工；已落库时直接采用 SQL 资料、权限和管理员关系，未落库才使用配置模拟资料。随后生成新随机 token，PUT 中央绑定 `userid + access_token`，成功后才返回。每次同用户显式登录均绑定新 token，由中央立即使旧 token 失效。

中央已有资料时只 PUT；仅既有契约的明确资料缺失 404 才由共用协调器读取完整权威表、发布 full-table，再用本次同一 token 重试一次 PUT。注册失败、非资料缺失错误不会伪造成功，不无限重写全表。详情见[注册协调](../capability/application/startup-user-sync.md)。

首次初始化仍归知识后端：解析唯一顶级部门、合法头像、可见平台（含离线）及自身初始 0 权限；已有员工不重置。缺失平台键代表未授权，不等同默认 0。增量资料、既有权限、SQL 管理员关系编辑和中央管理员增量维护只归 registry，本项目不调用 user/update 或管理员 add/remove。启动管理员全表由独立的[受控启动阶段](../capability/application/startup-admin-sync.md)负责，普通登录不发布管理员表。共同权威源与跨后端旧快照覆盖是[生产切换前提](../architecture/system/user-auth.md#中间件维护方授权与运维状态)。

上游超时可能发生在远端已提交之后，本方不会返回成功或声称已回滚；重新显式登录会生成新 token。应用重启不使中央有效 token 失效。

---

## 查询当前身份

`GET /knowledge-accumulation/api/auth/me`，两种模式下均只查询已有中央用户 token，不是开发登录入口。

### 认证方式

唯一 `Authorization: Bearer <user-access-token>`，使用 login 返回的随机用户 token。开发固定凭据不被本地识别为用户会话。

### 请求参数

无 path、query 参数，无 body。不能传 userid 指定身份，任何非空 body 均拒绝。

### 成功响应

`200 application/json`：

```json
{"userid":"developer","name":"开发模拟用户","employment_status":"employed","department_id":0,"department_name":"开发模拟部门","avatar_url":null,"platform_permissions":{"registry":0},"managed_platforms":[]}
```

employment_status 表示在职状态：employed 为在职、resigned 为离职。真实员工当前状态、权限和管理列表每次从权威仓库读取；开发 userid 已落库时同样读取 SQL，仅在中央可信 userid 匹配且 SQL 无记录时返回配置展示值，不写管理员表。头像只有文件名或 null，字段定义见[人员数据契约](../architecture/data/personnel.md#部门员工与管理员)。

### 错误响应

| 状态码 | detail 示例 | 触发条件 |
| --- | --- | --- |
| `401` | `用户 token 无效、过期或中央登录已离线` | Bearer 不合法、中央 userid=null 或员工不存在；含 WWW-Authenticate。 |
| `422` | `请求参数不合法` | 携带 query 或 body。 |
| `503` | `中间件接入未配置，用户认证暂不可用` / `中间件用户认证暂不可用` | 中间件接入未配置、数据库/中央/平台依赖故障或响应不符合契约。 |

依赖故障不向中央撤销用户 token；有效 token 在依赖恢复后可重试。

### 请求示例

替换为显式 login 返回的随机 token：

```bash
curl 'http://127.0.0.1:24000/knowledge-accumulation/api/auth/me' \
  -H 'Authorization: Bearer replace-with-central-user-access-token'
```

### 行为与边界

知识后端使用自己的平台 Bearer 向中央 POST，body 精确为 `{"access_token":"用户token"}`；中央确认 userid/null 并续期。知识后端不从本地 token 映射、客户端 userid 或固定开发凭据推导身份，不执行 PUT、全表注册、钉钉登录或权限维护，也不获取维护写锁。

中央失效或被新登录替换的旧 token 不能由 me 恢复；必须回到显式 login 重新交换。中央查询校验和故障规则见[会话规则](../architecture/system/user-auth.md#本地与中央会话规则)。


---

## 完整部门归属

认证用户对象增加 `departments` 数组，每项为 `{id, name}`，包含全部一级部门。`department_id`、`department_name`保留旧版兼容投影，不代表主部门；展示完整归属应使用数组。数据库历史单部门会读取为单项数组，权限和中央登录协议保持不变。
