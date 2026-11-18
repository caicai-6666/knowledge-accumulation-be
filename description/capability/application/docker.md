# Docker 构建与部署

## 镜像与启动

根目录 [Dockerfile](../../../Dockerfile) 使用 Python 3.12.15 slim bookworm，安装 [固定直接依赖](../../../requirement.txt) 并执行 `pip check`。安装 LibreOffice Writer/Calc/Impress 及系统字体，复制 `app/`、模型工具模板 `assets/tool-tag/` 和 `assets/fonts/` 中文字体；[.dockerignore](../../../.dockerignore) 排除实际配置、数据、测试和实验文件。

```bash
docker build -t knowledge-accumulation-be:dev .
```

服务以 UID/GID `10001:10001` 运行，工作目录 `/workspace`，监听 `0.0.0.0:8000`。启动命令固定 `uvicorn app.main:app --workers 1`；平台会话、任务执行、SSE 和缓存依赖进程内状态，不能直接增加 worker 或同时启动多个使用相同平台凭据的实例。

健康检查访问公开 `/openapi.json`，仅确认 HTTP 入口可用，不代表数据库、中央登录、钉钉或模型已经联通。数据库迁移不在镜像构建、容器启动或健康检查中执行；已启用的后台同步及记忆加工会查询业务 SQL。

---

## 本地开发入口

执行 `python -m app.main` 或在 IDE 中直接运行 `app/main.py`，监听 `127.0.0.1:15000`。入口在导入任何业务配置前设置 `APP_PATH_PREFIX=/dev`，关闭热重载时也使用相同前缀，业务 API 为 `/dev/knowledge-accumulation/api`；注册中心文件读取使用相同开发前缀，开发配置中的 `REGISTRY_BASE_URL` 应为 `http://127.0.0.1:16000`。

修改启动入口后需要重新启动已有进程；内存中已注册的路由不会随文件修改自动改变。该前缀与认证方式独立，`APP_ENV=production` 使用钉钉登录时仍可沿用本地开发入口和 `/dev`。

Docker/ASGI 默认不设置 `APP_PATH_PREFIX`，继续使用生产 API 路径和容器内部 `8000` 端口。开发网关和 Vite 必须保留 `/dev`，不能再剥除该前缀。

---

## Compose 装配

统一编排维护在 [knowledge-accumulation-deploy](../../../../knowledge-accumulation-deploy/readme.md)。正式部署时，本项目位于 deploy 内的 `knowledge-accumulation-be/`，其余子项目同样位于 deploy 内。知识后端服务名为 `knowledge-backend`，模板宿主端口为 `15000`，地址为 `http://127.0.0.1:15000/knowledge-accumulation/api`。

| 项目 | 当前行为 |
| --- | --- |
| 配置入口 | deploy 根 `.env` 是部署唯一入口，Compose 将 `KNOWLEDGE_*` 映射为知识后端对应变量；不再挂载或读取子项目 `.env`，不使用 `KNOWLEDGE_BACKEND_ENV_FILE`。 |
| 配置解析 | Compose 解析根配置并显式注入本服务所需环境变量。独立本地运行仍从本项目 `.env` 读取，进程环境优先。包含 `$`、空格或 JSON 的部署值按 deploy 模板使用单引号。 |
| 数据库 | 容器内连接 `mysql:3306`，账号、数据库与密码取共享 `MYSQL_*`；MySQL root 密码不进入后端。 |
| 注册中心文件 | `KNOWLEDGE_REGISTRY_BASE_URL` 模板为 `http://backend:8000`，文件来源数据库与主库使用同一部署配置。 |
| 中间件 | 使用根配置的 `MIDDLEWARE_BASE_URL` 及 `KNOWLEDGE_MIDDLEWARE_PLATFORM_CODE/SECRET`，密钥不进入前端。 |
| 文件 | `knowledge-backend-data` 卷挂载 `/workspace/data`；头像、转换后附件保存在该卷中。 |
| 访问 | `KNOWLEDGE_BACKEND_PORT` 控制宿主端口，默认绑定回环；容器内部监听 8000。 |

企业公开标识 `DINGTALK_CORP_ID` 注入知识后端和知识前端构建参数，其他后端凭据不进入前端镜像。修改该标识需要重建前端并重新创建后端。容器中的 `127.0.0.1` 指向容器自身；宿主服务需使用容器可达地址，本地配置中的地址不会自动转换。

助理心跳、正文转换、A2A 和模型参数均从根配置的 `KNOWLEDGE_*` 注入；全局心跳模板为 `KNOWLEDGE_ASSISTANT_HEARTBEAT_INTERVAL_SECONDS=300`，不再读取 assistants 表的逐员工间隔。具体参数以 deploy `.env.example` 和 Compose 映射为准，不保留第二份部署配置副本。

变更运行参数后使用 `docker compose up -d --no-build --wait knowledge-backend` 重新创建容器；单纯 restart 不刷新环境变量。变更代码或镜像依赖时使用 `docker compose up -d --build --wait knowledge-backend`。根 `.env` 保持私有，不输出完整 `docker compose config` 展开内容；只校验时使用 `config --quiet`。

中间件接入会同步用户与管理员全表，启用部署前须准备完整权威人员源并停止旧的同身份实例。配置 Embedding 地址后还会启动记忆加工；应用不自动迁移数据库。正文转换和工具失败分层无需新增容器或 SQL，见[工具失败处理](../../architecture/workflow/question-answering/tool-failures.md)。

---

## 数据与迁移

统一部署的新空卷由 [最新初始化脚本](../../../../knowledge-accumulation-deploy/mysql/init/001-schema.sql) 一次创建注册中心和知识后端当前全部业务表，包含 `schema/001–029` 的最终结构、全文索引、MyVector 缓存及画像序号初值。无需逐条执行历史迁移，也无需运行旧工作区迁移器。知识后端与 registry 使用同一权威人员库。

本项目已移除历史迁移 SQL，镜像不携带数据库脚本；历史定义仅从 Git 记录追溯。正式部署只使用 deploy 的最新初始化脚本。已有卷不会因重建镜像而变化；必须根据实际结构安排升级，不能重跑空库初始化，详见[部署数据库边界](../../../../knowledge-accumulation-deploy/mysql/readme.md#初始化与数据边界)。

上传文件元信息仅由个人助理登记，探索上传不登记到 `uploaded_files`。个人备忘新增、删除、查询、结果查看及个人助理附件访问校验已接入；没有历史批量回填。最终附件交付及钉钉入站顺序、取消状态均已纳入最新初始化。

引用用户/助理文字及富文本适配不新增部署变量或服务；应用代码随镜像打包，更新需执行 `docker compose up -d --build --wait knowledge-backend`，仅重启旧容器不会加载新代码。

命名卷首次创建时继承镜像中目录的 UID/GID；已有卷或自定义 bind 数据目录必须确保运行用户可写。新卷不会自动复制本机 `data/` 的头像和 PDF，实际切换需在停止写入、备份后迁移并核对文件引用。普通 `down` 保留卷，`down -v` 会删除卷。

---

## SSE 与运行边界

反向代理须保留 `/knowledge-accumulation/api` 路径，转发 `Authorization`，对 SSE 关闭响应缓冲并设置足够的读取超时；上传限制需覆盖原始附件及 multipart 请求大小。容器通过 `init: true` 转发信号，Compose 提供 1 分钟停止宽限，应用负责取消在途任务并释放连接。重启不能自动续跑进程内任务，公开历史恢复遵循[探索会话契约](../../architecture/workflow/question-answering/exploration-session.md)。

本次配置不自动启动共享服务、不执行真实数据库迁移、不复制实际 `.env` 或用户数据。镜像和隔离运行检查的结果不代表生产依赖已经就绪。

---

## 历史隔离验证

以下为各日期当时版本的验证记录，不代表当前新增 LibreOffice 的镜像已经重建。

2026-10-22 已完成实际镜像构建及镜像内 `pip check`。使用合成配置验证 Compose 默认配置和自定义端口、数据库、代理地址覆盖；检查两后端同源数据库、独立数据卷、只读配置挂载与 MySQL 健康依赖。

临时容器使用 `--network none`，数据显式挂载至宿主机 `tests/tmp/`，确认普通用户运行、运行依赖可导入、三个工具模板及十份 SQL 存在、实际配置和本地测试/实验数据不在镜像中。另验证挂载配置中的带引号 JSON 与 `$` 字符、数据目录可写、OpenAPI 健康以及 SIGTERM 后日志确认 `Application shutdown complete`（退出码 143）。临时容器和验证目录已清理，未连接真实数据库、中间件或模型，未启动部署项目的共享服务。

2026-10-30 配置同步：在现有挂载后端配置中补齐 6 个缺失的助理/A2A 参数，保留所有已有配置值；同步 deploy 模板注释、Compose 挂载说明和升级前提。实际配置通过 `AssistantRuntimeSettings`、`PlatformA2ASettings` 加载校验，生产和开发 Compose 均通过 `config --quiet`；解析生产 Compose 验证只读挂载目标及无额外助理/A2A/模型环境覆盖。未执行镜像重建、服务重启、数据库迁移或模型联通验证。

2026-11-09 部署初始化同步：deploy 合并为一份最新业务库 SQL，覆盖 26 张表及 001–029 最终结构。隔离 MySQL 对照迁移结果逐表验证通过；官方入口完成空库建表和 MyVector 安装，业务账号可访问，插件状态为 ACTIVE。生产及开发 Compose 配置校验通过，未改动现有开发库。


---

## 附件转换部署配置（当前）

deploy 使用根 `.env` 并由 Compose 显式注入环境变量。附件转换新增 `KNOWLEDGE_OFFICE_CONVERTER_EXECUTABLE=/usr/bin/soffice` 和 `KNOWLEDGE_PDF_MAX_VISUAL_TOKENS_PER_PAGE=2048`，分别映射为应用同名无 `KNOWLEDGE_` 前缀变量。实际部署规则以 [deploy 说明](../../../../knowledge-accumulation-deploy/readme.md#附件转换与探索上传) 为准。

图片、Word、Excel 转换依赖已纳入本项目 Dockerfile、requirement.txt 和字体构建白名单；无需新服务或数据库迁移。需重建 knowledge-backend 和适配后的 knowledge-frontend 镜像。探索上传网关请求体上限至少 22 MiB、注册读取超时至少 1800 秒；前端镜像 Nginx 当前读取超时为 3600 秒，外层网关需独立核对。本次仅同步配置，不启动部署服务。
