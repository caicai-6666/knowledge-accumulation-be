# 中间件平台注册与心跳

## 用途与接入边界

本模块使客户后端以 `knowledge-accumulation` 平台身份登录 `middleware-service`，并持续发送平台心跳。中间件现有白名单已包含该代号、名称“企业知识沉淀用户客户端”及独立密钥，本项目复用该身份。

平台注册采用“预配置白名单 + 平台登录”，没有动态创建平台的注册接口。本模块只维护平台会话，不维护管理员、不处理问答业务，也不将平台令牌作为用户登录凭据。另有启动自动执行的[用户全表注册协调](../application/startup-user-sync.md)复用本会话。

外部契约以中间件的[平台 API](../../../../../知识沉淀中间件服务/middleware-service/description/api/platform.md)及[平台身份配置](../../../../../知识沉淀中间件服务/middleware-service/description/capability/application/platform-config.md)为准；上述链接依赖当前多项目目录布局。

---

## 配置与启动

在项目根目录复制 [.env.example](../../../.env.example) 为 `.env`，填写中间件中对应平台的实际密钥。当前本机 `.env` 已配置现有中间件地址与该平台密钥，文件权限为 `0600`；由 [.gitignore](../../../.gitignore) 排除，不应提交。配置文件不执行 `$` 插值，进程环境变量优先于 `.env`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `MIDDLEWARE_BASE_URL` | 空 | HTTP/HTTPS 根地址，例如 `http://127.0.0.1:22000`；禁止凭据、接口路径、查询参数和 fragment。 |
| `MIDDLEWARE_PLATFORM_CODE` | `knowledge-accumulation` | 必须与中间件白名单的 `code` 完全一致。 |
| `MIDDLEWARE_SECRET` | 空 | 对应平台的独立密钥，保留原值，不裁剪或插值。 |
| `MIDDLEWARE_REQUEST_TIMEOUT_SECONDS` | `40` | 单次请求的总超时秒数，必须是有限正数；登录可能等待消息客户端初始化约 30 秒。 |
| `MIDDLEWARE_RETRY_INTERVAL_SECONDS` | `30` | 请求失败后的重试间隔秒数，必须是有限正数。 |

地址与密钥同时未配置时禁用接入，便于离线开发；仅提供其中一项或配置不合法时阻止应用启动。读取配置使用项目根目录的绝对定位，不依赖终端工作目录。

在项目根目录执行：

```bash
conda activate knowledge-accumulation-be
python -m pip install -r requirement.txt
python -m app.main
```

默认监听 `127.0.0.1:24000`，本地入口已开启热加载：保存 `app/` 下的 Python 文件后自动重启服务，包含工具及提示词代码。监视目录按入口文件绝对定位，不依赖 IDE 的工作目录；不监视 `data/`、测试和实验目录。现有未开启热加载的进程需要手动重启一次才能生效。

热加载复用现有 Uvicorn，不需要安装额外依赖；`.env` 和 `assets/` 不在此监视范围，修改后仍需手动重启。自动重启会结束当前进程，正在执行的问答及 SSE 会受影响，随后重新执行应用启动生命周期。

需要调整本地开发监听地址或端口时，在项目根目录执行：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 24000 --reload --reload-dir app
```

生产使用单 worker、不开启热加载的独立 CLI；Docker 沿用原生产命令：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 24000 --workers 1
```

生产环境应使用 HTTPS 访问中间件。密钥及令牌不进入前端或模型上下文。

---

## 实现与生命周期

| 位置 | 职责 |
| --- | --- |
| [配置模块](../../../app/config/middleware.py) | 加载并校验接入配置，使用 `SecretStr` 保存密钥，屏蔽配置报错中的原始输入。 |
| [HTTP 适配](../../../app/infrastructure/middleware.py) | 异步调用平台登录和心跳接口，校验状态码、平台代号、令牌类型和有效期。 |
| [会话服务](../../../app/service/middleware_session.py) | 单任务串行登录、心跳、重试与令牌管理，使用单调时钟判断有效期。 |
| [应用入口](../../../app/main.py) | 在 FastAPI lifespan 启动会话任务，在退出时取消任务并关闭客户端。 |

启动后后台调用 `POST /middleware-service/api/platform/login`，请求仅携带 `platform_code` 与 `secret`。成功后仅在内存保存令牌，按服务端返回的 `heartbeat_interval_seconds` 调用 `POST /middleware-service/api/platform/heartbeat`；心跳只携带 Bearer 认证头，无请求体。

响应必须为 `200`，平台代号必须匹配，建议心跳间隔必须小于会话有效期。登录响应还必须提供非空令牌和 `bearer` 类型。禁用重定向和环境代理，避免凭据被意外转发。

会话从请求开始时保守计算本地有效期，不把网络耗时计入额外存活时间。内部服务通过 `application.state.middleware_session_service.get_access_token()` 获取尚未过期的 `SecretStr` 或 `None`；读取不续期，不向前端暴露。后续业务调用收到 401 时，可用 `invalidate_access_token(token)` 失效相应令牌，迟到的旧请求不会清除新令牌。

网络异常、超时、响应契约错误和非成功状态均记录阶段、异常类型及 HTTP 状态码后重试，日志不输出响应正文、密钥或令牌。中间件暂不可用不阻塞 Web 服务启动，但不代表平台已在线。心跳收到 401 时清除令牌，等待重试后重新登录；短暂故障期间只在令牌尚未过期时允许内部读取。

---

## 单实例与停机边界

中间件规定同一平台只能有一个活跃会话。因此本项目当前必须以单实例、单 worker 运行；多实例或多个 worker 会竞争登录，无法共享进程内令牌。

遇到 `409` 保留中间件已有会话，本项目等待重试，不强制替换。应用重启、丢失令牌或登录响应丢失后，可能需要等待旧会话过期。目前中间件默认有效期 90 秒，实际以服务端配置为准。

中间件尚无登出接口。停止应用会取消心跳、清理本地令牌并关闭 HTTP 客户端；中间件中的在线标记会在会话自然过期后消失。

---

## 验证

用户会话 `POST /middleware-service/api/user/login-cache` 的响应现支持 `username`：有效登录返回当前用户名，无效登录返回 `null`。查询使用独立的严格 DTO，兼容旧版本省略该字段；类型错误、空名称或离线返回名称视为契约异常。`PUT` 仍使用不含用户名的原响应。用户名仅作展示资料，不替代可信 `userid`，不用于认定管理员或文件权限；本项目 `me` 仍读取人员数据库的完整身份。请求体依旧仅含用户 `access_token`，不新增资料写入。

[自动化测试](../../../tests/test_middleware.py)使用内存 HTTP Mock 和虚拟时钟，覆盖登录与无请求体心跳、响应校验、配置优先级及密钥保护、冲突与网络故障恢复、401 重登、过期判断、超时、禁用模式及 lifespan 清理。临时配置只写入 `tests/tmp/` 并清理，不读取真实 `.env` 或使用 `data/`。

```bash
python -m unittest discover -s tests -v
python -m pip check
```

此前平台接入阶段本机验证：12 项自动化测试通过；实际连接 `http://127.0.0.1:22000` 登录、心跳成功，返回有效期 90 秒、心跳间隔 30 秒；平台列表确认 `knowledge-accumulation` 的 `is_alive=true`。联调进程退出后已停止心跳，持续在线需要运行本应用。此次用户基础设施迁移仅做隔离测试，未重新连接共享中间件；结果见[全表注册验证](../application/startup-user-sync.md#验证与结果)。
