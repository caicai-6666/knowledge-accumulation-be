# 用户资源 API

提供已登录用户的头像及本人任务附件读取，复用中央 token 认证；不要求管理员身份。附件还需校验所属树及任务登记。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [读取头像](#读取头像) | `GET` | `/knowledge-accumulation/api/resource/avatar/{filename}` |
| [读取任务附件](#读取任务附件) | `GET` | `/knowledge-accumulation/api/resource/attachment/{tree_id}/{file_id}` |

---

## 读取头像

`GET /knowledge-accumulation/api/resource/avatar/{filename}`，读取配置头像目录内的 WebP 原始字节，不下载、不重新编码、不创建目录或登录。

### 认证方式

必须携带唯一 `Authorization: Bearer <user-access-token>`。复用普通中央 token 身份校验，中央确认 userid 后读取人员身份；任何已正常登录的普通用户均可读取合法头像，不按头像所有者、管理员名单或文件等级限制。开发固定凭据不能直接读取，应先通过 [login](auth.md#显式登录) 换取随机用户 token。

### 请求参数

| 位置 | 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- | --- |
| path | `filename` | string | 是 | 精确 32 位小写十六进制标识加 `.webp`，总长 37；禁止目录、其他后缀、尾随换行及控制字符。 |
| header | `Authorization` | string | 是 | 唯一用户 Bearer，不是中间件平台 token。 |
| header | `If-None-Match` | string | 否 | 上次响应的 ETag；支持逗号分隔列表、弱标签及 `*`，匹配时返回 304。 |

无 query、无 body；任何非空请求体或 query 返回 422。编码斜杠、子目录和无效名称不用于打开文件；客户端或代理预先规范化为其他路由的路径可能直接返回路由 404。

### 成功响应

`200 image/webp`，body 为存储文件原始二进制字节，包含：

```text
Cache-Control: private, max-age=86400
Vary: Authorization
ETag: "<头像内容的 SHA-256 十六进制摘要>"
X-Content-Type-Options: nosniff
```

浏览器可以私有缓存 24 小时，共享代理不得缓存；按 `Authorization` 区分缓存，凭据变化后不能直接复用旧凭据的缓存项。新头像保存时生成新文件名，前端取得新的头像 URL 后即可请求新图。缓存有效期内浏览器可以不访问后端，服务端会话失效或文件删除不会主动清除已经缓存的图片。

缓存过期或客户端主动重新验证时，可发送 `If-None-Match`。后端先完成身份校验及文件读取，再对内容计算 ETag；匹配返回 `304`，无正文，包含与 200 相同的缓存头、ETag 和 `Vary`，客户端复用缓存图片；不匹配返回 `200` 及完整图片。304 节省图片传输，不跳过后端认证和文件读取。不提供 `Last-Modified` 条件校验。

前端正常请求同一 URL 即可利用浏览器 HTTP 缓存，勿使用 `cache: 'no-store'` 或每次追加随机 URL 参数；本接口不接受 query。此变更不包含前端自行维护的 Blob 或应用内缓存。

不提供重定向或公开静态目录，不将内部路径写入响应。读取字节受 `AVATAR_MAX_DOWNLOAD_BYTES` 限制，总读取超时使用 `AVATAR_TIMEOUT_SECONDS`；配置和实现见[头像下载与安全存储](../capability/infrastructure/user-infrastructure.md#头像下载与安全存储)。

### 错误响应

错误媒体类型为 `application/json`，均使用脱敏 detail 和 no-store：

| 状态码 | detail 示例 | 条件 |
| --- | --- | --- |
| `401` | `用户 token 无效、过期或中央登录已离线` | Bearer 缺失/不合法、中央失效/被替换或员工不存在。 |
| `404` | `头像不存在` | 合法名称对应文件或头像根目录不存在。 |
| `422` | `请求参数不合法` | 名称、路径、body 或 query 不合法，不回显输入。 |
| `503` | `中间件接入未配置，用户认证暂不可用` | 未配置中间件接入时沿用认证依赖失败，不读取头像或隐式登录。 |
| `503` | `头像读取暂不可用` | 文件/根目录符号链接、非普通文件、超限、读取故障、超时或 Store 不可用。 |
| `503` | `中间件用户认证暂不可用` | 平台会话、中央、数据库故障或非法认证响应，具体 detail 按既有认证契约脱敏。 |

中央平台 401 映射依赖 503；用户 token 明确失效为 401。认证失败时不读取头像、不 PUT、不发布 full-table；依赖故障不撤销中央用户 token。

### 请求示例

替换地址、文件名和实际随机用户 token：

```bash
curl 'http://127.0.0.1:24000/knowledge-accumulation/api/resource/avatar/0123456789abcdef0123456789abcdef.webp' \
  -H 'Authorization: Bearer replace-with-central-user-access-token' \
  -o avatar.webp
```

### 行为与边界

[资源路由](../../app/router/resource.py)校验名称后调用同一应用 `AvatarStore.read('user/avatar/'+filename)`。Store 通过目录描述符读取，根目录和文件拒绝符号链接，文件必须为普通文件，读取最多上限加一字节以检测超限。读取异常仅记录类型，不输出原始异常、路径、token 或文件内容。

已配置中间件接入时，开发与生产各应用共用一个 Store，生产 onboarding 也复用它；停止时关闭一次。登录无独立开关，用户/管理员全表改为启动自动同步，不设独立开关；中央 token 最终契约不变。头像数据迁移需用户单独授权，不通过该 GET 接口执行。


---

## 读取任务附件

`GET /knowledge-accumulation/api/resource/attachment/{tree_id}/{file_id}`，按本人问答树内的任务附件登记返回原始文件字节，供前端展示或保存。

### 认证方式

必填 `Authorization: Bearer <access_token>`，每次请求查询中央会话。使用显式登录得到的随机用户 token，开发固定凭据不能用于读取；不接受 query token，不隐式登录。普通用户可以读取自己的附件，不要求管理员或平台文件等级；其他用户的树及未登记附件统一 `404`。此资源能力按树归属校验，不局限于 exploration 类型。

### 请求参数

| 位置 | 参数 | 必填 | 约束 |
| --- | --- | --- | --- |
| path | `tree_id` | 是 | 附件所属树 ID，32 位小写十六进制字符。 |
| path | `file_id` | 是 | 打开树返回的 `content.attachments[].file_id`，36 位小写带连字符 UUID；不包含目录或后缀。 |

无 query、无请求体；多余参数返回 `422`。UUID 只是引用，不能替代用户认证或树归属校验。类型与文件后缀由数据库登记决定，客户端不能指定；路径被客户端预先规范化为其他路由时可能返回路由 `404`。

### 成功响应

`200`，body 为原始文件字节：

| 登记类型 | Content-Type | 固定后缀 |
| --- | --- | --- |
| `images` | `application/pdf` | `.pdf` |
| `audio` | `audio/mpeg` | `.mp3` |
| `video` | `video/mp4` | `.mp4` |

```text
Cache-Control: no-store
X-Content-Type-Options: nosniff
Content-Disposition: inline; filename="<file_id>.<固定后缀>"
```

当前探索上传接口仅接受 PDF；此读取接口按既有三类存储登记映射媒体类型，不提供转码或新增音视频上传能力。不使用公开静态目录、重定向或条件缓存；当前整文件返回，不实现 Range 分段响应。

### 错误响应

| 状态码 | detail 示例 | 条件 |
| --- | --- | --- |
| `401` | `用户 token 无效、过期或中央登录已离线` | token 缺失、不合法、失效或被新登录替换。 |
| `404` | `附件不存在` | 树不存在、非本人、附件未登记、登记被删除或磁盘文件缺失。 |
| `422` | `请求参数不合法` | 树 ID、文件 ID、query 或请求体不合法。 |
| `503` | `附件读取暂不可用` | 服务未装配、数据库/读取故障、超时、符号链接、非普通文件、空文件或超限。 |
| `503` | 按现有认证契约脱敏 | 中央会话、平台会话或身份仓库不可用；不读取文件。 |

### 请求示例

替换树 ID、附件 UUID、用户 token 和服务地址：

```bash
curl 'http://127.0.0.1:24000/knowledge-accumulation/api/resource/attachment/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/abcdefab-1234-4234-8234-123456789abc' \
  -H 'Authorization: Bearer replace-with-central-user-access-token' \
  -o attachment.pdf
```

前端携带 Authorization 使用 fetch 获取 Blob，再自行展示或保存；普通裸链接不会自动携带用户 Bearer。

### 行为与边界

应用组合根复用同一个 `QuestionFileService` 给问答生成和读取路由。读取经 `read_file` 核实本人树内的附件登记，使用 `UploadFileStore` 安全读取，再核对登记仍然有效；不从客户端路径直接读取磁盘，不构造模型客户端。整条读取链路限时 15 秒，单文件沿用 Store 上限（默认 50 MiB）；根目录与各级路径拒绝符号链接，不创建缺失目录。

授权、存储、删除协调和配置说明见[用户附件引用与文件定位](../architecture/data/question-file.md)。本接口无新增环境变量或数据库迁移，不修改实际 `.env` 或真实文件。[接口测试](../../tests/test_attachment_resource.py)使用隔离中央响应、仓库替身及 `tests/tmp` 文件，覆盖 MIME、归属、中央失效、非法路径、安全读取和删除后的二次校验。
