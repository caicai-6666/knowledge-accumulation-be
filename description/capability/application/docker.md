# Docker 构建与部署

## 镜像与启动

根目录 [Dockerfile](../../../Dockerfile) 使用 Python 3.12.15 slim bookworm，安装 [固定直接依赖](../../../requirement.txt) 并执行 `pip check`。只复制 `app/`和模型工具模板 `assets/tool-tag/`；[.dockerignore](../../../.dockerignore) 排除实际配置、数据、测试和实验文件。

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

统一部署配置位于同级 [knowledge-accumulation-deploy](../../../../knowledge-accumulation-deploy/readme.md)，服务名 `knowledge-backend`，默认入口为 `http://127.0.0.1:24000/knowledge-accumulation/api`。

| 项目 | 行为 |
| --- | --- |
| 配置 | `KNOWLEDGE_BACKEND_ENV_FILE` 指向后端实际 `.env`，只读挂载到 `/workspace/.env`，默认 `../knowledge-accumulation-be/.env`。 |
| 解析 | 应用以 `python-dotenv` 且 `interpolate=False` 读取文件，支持示例中的带引号 JSON，不把整个文件作为 Compose `env_file.format: raw` 传入。 |
| 数据库 | Compose 环境覆盖为 `mysql:3306`，数据库、账号和密码与 registry 使用同一部署配置。 |
| 注册中心文件 | `REGISTRY_BASE_URL` 固定为 `http://backend:8000`；`FILE_SOURCE_MYSQL_HOST/PORT` 为 `mysql:3306`，数据库、账号与密码复用部署的 `MYSQL_*`，覆盖挂载文件中的本机连接项。 |
| 中间件 | 覆盖为部署配置中的 `MIDDLEWARE_BASE_URL`，平台身份固定 `knowledge-accumulation`；密钥仍取知识后端自己的配置。 |
| 文件 | 独立命名卷 `knowledge-backend-data` 挂载 `/workspace/data`，头像路径固定 `/workspace/data/user/avatar`，附件位于 `/workspace/data/user/upload-file`。 |
| 访问 | 宿主机端口由 `KNOWLEDGE_BACKEND_PORT` 控制，默认只绑定回环地址；registry 头像代理默认访问 `http://knowledge-backend:8000`。 |

环境变量优先于文件；Compose 中的企业公开标识统一取部署 `.env` 的 `DINGTALK_CORP_ID`，同时注入知识后端和前端构建参数 `VITE_DINGTALK_CORP_ID`。`APP_ENV`、AppKey、AppSecret、开发登录凭据、模型地址及密钥仍从知识后端配置读取，密钥不进入前端构建。生产应设置 `APP_ENV=production`，并提供共享 CorpId 及后端 AppKey/AppSecret；开发则配置固定登录凭据。共享 CorpId 留空不会回退到后端文件值，修改后需重建前端并重新创建知识后端；独立运行后端时仍从本项目 `.env` 读取 CorpId。

中间件接入后会自动执行用户及管理员全表同步，启动前必须准备完整权威人员源，并停止旧的同身份运行实例。跨后端旧快照覆盖并发权限更新的边界仍需由部署侧保证。配置 `VLLM_EMBEDDING_BASE_URL` 后另会启动[记忆自动向量化](memory-embedding.md)，须预先准备问答表和向量服务；空地址不装配该任务。

个人助理及平台 A2A 配置也从 `KNOWLEDGE_BACKEND_ENV_FILE` 读取，不在 deploy `.env` 或 Compose 环境中维护第二份值。部署模板已提示同步 `ASSISTANT_MODEL_RETRY_SECONDS=30`、`ASSISTANT_RENDERING_THINKING_EFFORT=medium` 及 A2A 响应缓冲、轮询、HTTP 超时和并发上限；其他开关、模型地址与自定义值保留；已有转换档位若为 `none`，需显式改为 `medium` 才会开启推理。部署侧配置清单见[助理、转换钩子与平台协作](../../../../knowledge-accumulation-deploy/readme.md#助理转换钩子与平台协作)。

启用助理时，[转换子智能体](../../architecture/workflow/question-answering/assistant-message-rendering.md)作为 `finish_task` 独立钩子自动装配，共用 `VLLM_MLLM_*`，转换默认开启 `medium` 原生推理，要求模型服务支持 `/tokenize`；通过提示生成 JSON 并由本地 Pydantic 严格校验，不发送接口级 `response_format`。转换正文在普通提交前替换，失败不会自动降级发送原文；无需新增容器、端口或 SQL 迁移。配置文件变更需重新创建/启动后端才会加载，镜像仍通过 `COPY app` 包含钩子实现。

Linux 主机上，挂载的配置文件必须允许容器 UID/GID `10001` 读取。可准备仅部署用户和 GID `10001` 可读的独立配置副本（如权限 `0640`），将 `KNOWLEDGE_BACKEND_ENV_FILE` 指向该副本；不要公开配置内容或把权限 `0600` 且属于其他用户的文件直接视为容器可读。路径缺失时 Compose 拒绝自动创建同名目录。

容器中的 `127.0.0.1` 指向容器自己。中间件位于宿主机时可使用 `host.docker.internal`，宿主服务须监听容器可达接口；MLLM/Embedding 地址也必须可从容器访问。`.env` 中既有本机地址不会自动转换。已有部署 `.env` 如果显式设置了旧 `KNOWLEDGE_ACCUMULATION_BASE_URL`，需要在实际部署时改为新服务地址，默认值不会覆盖已有配置。

---

## 数据与迁移

统一部署的新空卷由 [最新初始化脚本](../../../../knowledge-accumulation-deploy/mysql/init/001-schema.sql) 一次创建注册中心和知识后端当前全部业务表，包含 `schema/001–029` 的最终结构、全文索引、MyVector 缓存及画像序号初值。无需逐条执行历史迁移，也无需运行旧工作区迁移器。知识后端与 registry 使用同一权威人员库。

本项目已移除历史迁移 SQL，镜像不携带数据库脚本；历史定义仅从 Git 记录追溯。正式部署只使用 deploy 的最新初始化脚本。已有卷不会因重建镜像而变化；必须根据实际结构安排升级，不能重跑空库初始化，详见[部署数据库边界](../../../../knowledge-accumulation-deploy/mysql/readme.md#初始化与数据边界)。

上传文件元信息仅由个人助理登记，探索上传不登记到 `uploaded_files`。个人备忘新增、删除、查询、结果查看及个人助理附件访问校验已接入；没有历史批量回填。最终附件交付及钉钉入站顺序、取消状态均已纳入最新初始化。

引用用户/助理文字及富文本适配不新增部署变量或服务；应用代码随镜像打包，更新需执行 `docker compose up -d --build --wait knowledge-backend`，仅重启旧容器不会加载新代码。

命名卷首次创建时继承镜像中目录的 UID/GID；已有卷或自定义 bind 数据目录必须确保运行用户可写。新卷不会自动复制本机 `data/` 的头像和 PDF，实际切换需在停止写入、备份后迁移并核对文件引用。普通 `down` 保留卷，`down -v` 会删除卷。

---

## SSE 与运行边界

反向代理须保留 `/knowledge-accumulation/api` 路径，转发 `Authorization`，对 SSE 关闭响应缓冲并设置足够的读取超时；上传限制需覆盖应用允许的 PDF 请求大小。容器通过 `init: true` 转发信号，Compose 提供 1 分钟停止宽限，应用负责取消在途任务并释放连接。重启不能自动续跑进程内任务，公开历史恢复遵循[探索会话契约](../../architecture/workflow/question-answering/exploration-session.md)。

本次配置不自动启动共享服务、不执行真实数据库迁移、不复制实际 `.env` 或用户数据。镜像和隔离运行检查的结果不代表生产依赖已经就绪。

---

## 隔离验证

2026-10-22 已完成实际镜像构建及镜像内 `pip check`。使用合成配置验证 Compose 默认配置和自定义端口、数据库、代理地址覆盖；检查两后端同源数据库、独立数据卷、只读配置挂载与 MySQL 健康依赖。

临时容器使用 `--network none`，数据显式挂载至宿主机 `tests/tmp/`，确认普通用户运行、运行依赖可导入、三个工具模板及十份 SQL 存在、实际配置和本地测试/实验数据不在镜像中。另验证挂载配置中的带引号 JSON 与 `$` 字符、数据目录可写、OpenAPI 健康以及 SIGTERM 后日志确认 `Application shutdown complete`（退出码 143）。临时容器和验证目录已清理，未连接真实数据库、中间件或模型，未启动部署项目的共享服务。

2026-10-30 配置同步：在现有挂载后端配置中补齐 6 个缺失的助理/A2A 参数，保留所有已有配置值；同步 deploy 模板注释、Compose 挂载说明和升级前提。实际配置通过 `AssistantRuntimeSettings`、`PlatformA2ASettings` 加载校验，生产和开发 Compose 均通过 `config --quiet`；解析生产 Compose 验证只读挂载目标及无额外助理/A2A/模型环境覆盖。未执行镜像重建、服务重启、数据库迁移或模型联通验证。

2026-11-09 部署初始化同步：deploy 合并为一份最新业务库 SQL，覆盖 26 张表及 001–029 最终结构。隔离 MySQL 对照迁移结果逐表验证通过；官方入口完成空库建表和 MyVector 安装，业务账号可访问，插件状态为 ACTIVE。生产及开发 Compose 配置校验通过，未改动现有开发库。
