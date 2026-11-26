# Docker 构建与部署

## 镜像与启动

根目录 [Dockerfile](../../../Dockerfile) 使用 `python:3.12.15-slim-bookworm`，系统层仅安装 `git` 与 `ca-certificates`（用于拉取基于 Git 的 `agent-core[mysql,pdf]` 依赖），随后安装 [requirement.txt](../../../requirement.txt) 并执行 `pip check`。附件转 PDF 与网页抓取/外部专家能力已迁移至独立的 `knowledge-accumulation-mcp-server`，镜像不再安装 LibreOffice、系统字体或复制 `assets/fonts/`，仅复制 `app/` 与 `assets/tool-tag/`；[.dockerignore](../../../.dockerignore) 排除实际配置、运行数据、测试和实验文件。

```bash
docker build -t knowledge-accumulation-be:dev .
```

服务以 UID/GID `10001:10001` 运行，工作目录 `/workspace`，监听 `0.0.0.0:8000`。启动命令固定 `uvicorn app.main:app --workers 1`；平台会话、任务执行、SSE 和缓存依赖进程内状态，不能直接增加 worker 或同时启动多个使用相同平台凭据的实例。

健康检查访问公开 `/openapi.json`，仅确认 HTTP 入口可用，不代表数据库、中央登录、MCP Server、钉钉或模型已经联通。数据库建表不在镜像构建、容器启动或健康检查中执行。

---

## 本地开发入口

执行 `python -m app.main` 或在 IDE 中直接运行 `app/main.py`，监听 `127.0.0.1:15000`。入口在导入任何业务配置前设置 `APP_PATH_PREFIX=/dev`，关闭热重载时也使用相同前缀，业务 API 为 `/dev/knowledge-accumulation/api`；注册中心文件读取使用相同开发前缀，开发配置中的 `REGISTRY_BASE_URL` 应为 `http://127.0.0.1:16000`。

修改启动入口后需要重新启动已有进程；内存中已注册的路由不会随文件修改自动改变。该前缀与认证方式独立，`APP_ENV=production` 使用钉钉登录时仍可沿用本地开发入口和 `/dev`。

Docker/ASGI 默认不设置 `APP_PATH_PREFIX`，继续使用生产 API 路径和容器内部 `8000` 端口。开发网关和 Vite 必须保留 `/dev`，不能再剥除该前缀。

---

## Compose 装配

统一编排维护在 `knowledge-accumulation-deploy` 仓库。正式部署时，本项目位于 deploy 内的 `knowledge-accumulation-be/`，知识后端服务名为 `knowledge-backend`，模板宿主端口为 `15000`，业务地址为 `http://127.0.0.1:15000/knowledge-accumulation/api`。

| 项目 | 当前行为 |
| --- | --- |
| 配置入口 | deploy 根 `.env` 是部署唯一入口，Compose 将 `KNOWLEDGE_*` 映射为知识后端对应变量；不再挂载或读取子项目 `.env`。 |
| 配置解析 | Compose 解析根配置并显式注入本服务所需环境变量。独立本地运行仍从本项目 `.env` 读取，进程环境优先。包含 `$`、空格或 JSON 的部署值使用单引号。 |
| 数据库 | 容器内连接 `mysql:3306`，账号、数据库与密码取共享 `MYSQL_*`；MySQL root 密码不进入后端。 |
| MCP 工具服务 | 通过 `MCP_ENABLED`、`MCP_SERVER_URL` 与 `MCP_TIMEOUT_SECONDS` 连接外部 `knowledge-accumulation-mcp-server`（Streamable HTTP），承担时间/日期、网页检索、外部专家与附件转 PDF。 |
| 注册中心文件 | `KNOWLEDGE_REGISTRY_BASE_URL` 模板为 `http://backend:8000`，文件来源数据库与主库使用同一部署配置。 |
| 中间件 | 使用根配置的 `MIDDLEWARE_BASE_URL` 及 `KNOWLEDGE_MIDDLEWARE_PLATFORM_CODE/SECRET`，密钥不进入前端。 |
| 文件卷 | `knowledge-backend-data` 卷挂载 `/workspace/data`；头像与上传附件保存在该卷中。 |
| 访问 | `KNOWLEDGE_BACKEND_PORT` 控制宿主端口，默认绑定回环；容器内部监听 `8000`。 |

容器中的 `127.0.0.1` 指向容器自身；宿主机上的中间件或 MCP Server 需使用容器可达地址（如 `host.docker.internal` 或同网段服务名）。变更运行参数后使用 `docker compose up -d --no-build --wait knowledge-backend` 重建容器；变更代码或镜像依赖时使用 `docker compose up -d --build --wait knowledge-backend`。根 `.env` 保持私有，校验配置时使用 `docker compose config --quiet`。

中间件接入会同步用户与管理员全表，启用部署前须准备完整权威人员源并停止旧的同身份实例。

---

## 数据与迁移

新空库部署时，必须显式初始化以下两类表结构（应用不自动执行 DDL）：

| 类别 | 表清单 | 来源与说明 |
| --- | --- | --- |
| `agent-core` 核心表（5 张） | `agent_core_schema`、`records`、`spaces`、`memory`、`file_notes` | 由 `agent_core.schema_sql("mysql")` 生成，承载探索与个人助理的任务树、工作区、压缩窗口、长期记忆、文件备忘及定时提醒/心跳状态。 |
| 探索与助理树元数据表（3 张） | `exploration_trees`、`exploration_tree_files`、`assistant_tree_files` | 由 `app.repository.exploration_tree.exploration_schema_sql("mysql")` 与助理业务 DDL 提供，维护用户归属与附件授权映射；主干 `head`、节点耗时与 `display:<task_id>` 由 `agent-core` `records` 表原生承载。 |
| 人员与注册中心共享表 | `departments`、`employees`、`platform_admins`、`platform_permission_control`、`resources` | 与 `registry-be` 共享权威人员与文件目录源；`platform_permission_control` 需初始化 `id=1` 种子行。 |
| 个人助理账本与协作表 | `assistants`、`assistant_task_executions`、`assistant_deliveries`、`assistant_portraits`、`assistant_portrait_sequence`、`assistant_portrait_actions`、`assistant_file_notes`、`uploaded_files`、`assistant_robot_inbox`、`platform_a2a_calls` | 承载个人助理绑定、任务执行租约、可靠交付 Outbox、员工私有画像、附件元信息、钉钉入站收件箱与跨平台 A2A 账本。 |

**已废弃的旧表不再使用**：旧版 `question_trees`、`question_nodes`、`question_node_contents`、`question_node_generations`、`question_node_memories`、`question_node_messages`、`question_workspaces`、`question_memory_index`、`question_tree_deletions`、`myvector_documents`、`assistant_workspaces` 与 `assistant_reminders` 均已被 `agent-core` 5 张核心表与上述轻量业务表取代，新空库无需再创建，也不再依赖 MyVector 插件。

命名卷首次创建时继承镜像中 `/workspace/data` 目录的 UID/GID（`10001:10001`）；已有卷或自定义 bind 数据目录必须确保运行用户可写。普通 `down` 保留卷，`down -v` 会删除卷。

---

## 跨仓库部署待办

以下事项涉及相邻仓库，须在正式发布前由对应仓库同步完成（本仓库不直接修改外仓文件）：

1. **`knowledge-accumulation-deploy`**：
   - 更新 `mysql/init/001-schema.sql`：纳入 `agent-core` 5 张核心表及 `exploration_trees` / `exploration_tree_files` / `assistant_tree_files`，移除已废弃的旧 `question_*` / `myvector_documents` / `assistant_workspaces` / `assistant_reminders` 表定义。
   - 更新 `docker-compose.yaml` 与 `.env.example`：为 `knowledge-backend` 补充 `MCP_ENABLED`、`MCP_SERVER_URL`、`MCP_TIMEOUT_SECONDS` 环境变量映射，移除已清理的 `DEEPSEEK_*`、`AGENT_*`、`MEMORY_EMBEDDING_*`、`OFFICE_CONVERTER_EXECUTABLE`、`PDF_MAX_VISUAL_TOKENS_PER_PAGE` 变量。
2. **`knowledge-accumulation-mcp-server`**：
   - 官方 `mcp>=2.3.0` 默认开启 DNS rebinding 保护（仅放行 `127.0.0.1:*` / `localhost:*`），业务容器经 `http://host.docker.internal:22100/mcp` 直连会返回 `421 Invalid Host header`。容器化联调前需在 MCP Server 配置 `TransportSecuritySettings(allowed_hosts=[..., "host.docker.internal:*"])`（或对应容器服务名），或在同网段通过反向代理改写 `Host` 头。

---

## SSE 与运行边界

反向代理须保留 `/knowledge-accumulation/api` 路径，转发 `Authorization`，对 SSE 关闭响应缓冲并设置足够的读取超时；上传网关请求体上限至少 22 MiB，探索注册与 SSE 读取超时需覆盖远程附件转 PDF 与外部专家调用耗时。容器通过 `init: true` 转发信号，Compose 提供 1 分钟停止宽限，应用负责取消在途任务并释放连接。重启不能自动续跑进程内任务，公开历史恢复遵循[探索会话契约](../../architecture/workflow/question-answering/exploration-session.md)。
