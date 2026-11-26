# 企业知识沉淀客户后端

`knowledge-accumulation-be` 为企业知识沉淀前端 `knowledge-accumulation-fe` 提供身份认证、探索问答和个人助理服务。项目基于 Python 3.12、FastAPI、独立 `agent-core` 运行时库、外部 MCP 工具服务与 MySQL，通过中间件连接平台会话、任务队列及跨平台协作能力，并通过钉钉机器人提供员工个人助理入口。

[项目总览](description/project.md) · [文档导航](description/readme.md) · [API 参考](description/api/readme.md) · [开发规范](AGENTS.md)

---

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 用户与身份 | 钉钉登录、开发登录、中央会话验证、员工及管理员全表同步、头像与平台信息查询。 |
| 探索问答 | 基于 `agent-core`（`interruptible` + `per_node`）实现问答树管理、主干推进与历史节点分叉、附件上传、任务注册、SSE 实时事件流、取消与公开历史恢复。 |
| 个人助理 | 基于 `agent-core`（`queue` + `latest`）实现钉钉单聊接收、队列消费、员工背景、私有画像、助理间消息、钉钉日历、内置定时提醒与空闲心跳调度。 |
| 通用 MCP 工具 | 通过官方 MCP Streamable HTTP 接入 `knowledge-accumulation-mcp-server`，提供当前时间、日期计算、网页搜索与正文读取、外部专家问答及多格式附件转 PDF。 |
| 业务资料与检索 | 注册中心文件查询及授权阅读、本地/远程 PDF 逐页查看、`agent-core` 祖先记忆召回与文件备忘。 |
| 平台协作 | 通过中间件 A2A 队列广播问题、接收反馈和继续交流，可按配置启用。 |

探索与个人助理共用 `agent-core` 运行时内核（`AgentRuntime` / `TreeAgent`），由各自装配服务注入业务提示词、本地工具与 MCP 适配工具；工具反馈统一经末尾 `FeedbackOnlyHook` 渲染为面向模型的中文正文。个人助理的 `finish_task` 默认通过正文转换子智能体生成适合钉钉的 Markdown 正文，详细契约见[助理正文转换](description/architecture/workflow/question-answering/assistant-message-rendering.md)。

---

## 快速开始

### 1. 准备环境

使用 Python 3.12，在项目根目录创建隔离环境并安装依赖（安装 `agent-core` 需要本机可用 `git`）：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirement.txt
python -m pip check
```

也可使用 Conda，见 [Python 环境与依赖](description/capability/application/dependencies.md)。本项目不再依赖本机安装 LibreOffice，多格式附件转 PDF 由外部 `knowledge-accumulation-mcp-server` 承担。

### 2. 配置依赖

首次运行时从模板创建 `.env`；已有文件则保留：

```bash
if [ ! -e .env ]; then
    cp .env.example .env
fi
```

编辑 `.env`，替换地址、账号和凭据占位值。应用默认从项目根目录加载 `.env`，进程环境变量优先，不展开 `${...}`。完整参数见 [.env.example](.env.example)，首次接入重点配置：

| 配置 | 用途与要求 |
| --- | --- |
| `MIDDLEWARE_BASE_URL`、`MIDDLEWARE_SECRET` | 中间件地址及本平台密钥；平台代码固定为 `knowledge-accumulation`。登录、平台目录和助理队列依赖该会话。 |
| `APP_ENV`、`AUTH_DEVELOPER_ACCESS_TOKEN` | 本地登录使用 `development` 和非空固定登录凭据；该凭据用于换取中央用户 token。 |
| `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD` | 配置已初始化的 MySQL 8 数据库（含 `agent-core` 5 表与本项目业务表），人员数据须与 registry 使用同一权威源。 |
| `MCP_ENABLED`、`MCP_SERVER_URL` | 外部 `knowledge-accumulation-mcp-server` 的 Streamable HTTP 地址（默认 `http://127.0.0.1:22100/mcp`）；探索与助理的通用工具及附件转 PDF 均依赖该服务。 |
| `VLLM_MLLM_BASE_URL`、`VLLM_MLLM_MODEL` | 主模型地址与实际服务模型名；按服务配置 API key 和工具格式模板。 |
| `VLLM_EMBEDDING_BASE_URL` | 向量模型地址；供 `agent-core` 记忆向量检索及注册中心文件摘要检索使用，留空时禁用向量路径。 |
| `REGISTRY_BASE_URL`、`FILE_SOURCE_MYSQL_*` | 注册中心原件服务与文件检索数据源，按需配置。 |
| `ASSISTANT_ENABLED`、`PLATFORM_A2A_ENABLED` | 默认均为 `false`，完成对应依赖与建表后再开启。 |

主模型服务需支持原生工具调用和 `/tokenize`；模型名、上下文容量和工具模板需与实际服务一致，见[模型配置](description/capability/infrastructure/vllm-models.md)。仅启动身份接口且关闭助理时，主模型地址可暂留空。

中间件接入后会自动同步用户及管理员全表，启动前应准备完整权威人员源。数据库表结构由部署侧显式初始化，应用不会自动执行 DDL；最新表结构要求与迁移边界见[数据与迁移](description/capability/application/docker.md#数据与迁移)。

如只查看 Web 入口和 OpenAPI，可同时清空中间件地址及密钥，保持助理、A2A 关闭并留空 Embedding 地址；这不提供可用的登录和完整问答服务。

### 3. 启动本地服务

启动前请确认依赖的中间件、`knowledge-accumulation-mcp-server`、MySQL 及模型服务已就绪，然后运行：

```bash
python -m app.main
```

| 入口 | 本地地址 |
| --- | --- |
| 业务 API | `http://127.0.0.1:15000/dev/knowledge-accumulation/api` |
| Swagger UI | `http://127.0.0.1:15000/docs` |
| OpenAPI | `http://127.0.0.1:15000/openapi.json` |

该入口与 IDE 直接运行 `app/main.py` 一致：端口为 `15000`，导入路由前设置 `/dev` 前缀，默认关闭热重载。修改代码、提示词或配置后需重启。`APP_ENV` 控制认证方式，与 URL 前缀独立。

确认 HTTP 入口可访问：

```bash
curl --fail http://127.0.0.1:15000/openapi.json
```

OpenAPI 可访问仅说明 Web 入口正常；登录、数据库、MCP 与模型仍需分别验证。开发登录流程见[认证 API](description/api/auth.md)，探索任务的注册、订阅激活和取消顺序见[探索问答 API](description/api/exploration.md)。

---

## 启用钉钉个人助理

准备中间件队列、MCP Server、已初始化的 `agent-core` 核心表与助理业务表及可用模型服务后，配置 `DINGTALK_CORP_ID`、`DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET` 和 `DINGTALK_ROBOT_CODE`，并设置 `ASSISTANT_ENABLED=true`。应用会同时装配 Stream 入站、任务消费、可靠交付以及基于 `agent-core` 的定时提醒与空闲心跳调度。生产登录另需设置 `APP_ENV=production`。

钉钉后台需完成机器人发布、可见范围及权限配置，具体步骤见[机器人接入](description/capability/infrastructure/dingtalk-robot.md)。最终正文转换使用同一 MLLM 服务，默认开启 `medium` 原生推理（`ASSISTANT_RENDERING_THINKING_EFFORT=medium`），通过提示生成 JSON 并由本地 Pydantic 严格校验；转换失败时不会发送原文，由主模型按失败回执继续处理。

---

## Docker 部署

项目提供 [Dockerfile](Dockerfile)，可在根目录构建：

```bash
docker build -t knowledge-accumulation-be:dev .
```

镜像使用 Python 3.12 slim，仅安装 `git` 与 `ca-certificates`（不再包含 LibreOffice 及字体包），以非 root 用户运行，容器内部监听 `8000`，固定单 worker。统一 Compose 编排维护在 `knowledge-accumulation-deploy` 项目；正式部署时本项目位于其 `knowledge-accumulation-be/` 子目录，模板暴露宿主端口 `15000`，业务地址为 `http://127.0.0.1:15000/knowledge-accumulation/api`。

Docker / ASGI 导入默认不带 `/dev`；`APP_PATH_PREFIX` 通过进程环境在路由导入前设置。部署统一读取 deploy 根 `.env`，由 Compose 显式注入知识后端环境变量，运行数据使用持久卷；容器内访问外部 MCP Server 需配置容器可达地址并确保 MCP Server 放行对应 Host。镜像不包含本机凭据和用户数据，也不会自动执行建表。详细步骤、表结构清单、跨仓库部署待办及 SSE 代理要求统一见[Docker 构建与部署](description/capability/application/docker.md)。

---

## 目录结构

```text
app/
├── main.py              # 应用入口与生命周期装配
├── agent/               # 探索模式、个人助理装配、MCP 适配与业务工具
├── auth/                # 身份认证适配
├── config/              # 配置加载与校验
├── infrastructure/      # MCP 客户端、中间件、钉钉与文件存储适配
├── model/               # 业务数据模型
├── repository/          # agent-core 存储协调与业务 SQL 仓库
├── router/              # HTTP 与 SSE 路由
├── schema/              # API 请求与响应模型
└── service/             # 业务编排、运行时装配、队列消费与交付
assets/tool-tag/         # 模型工具格式模板
description/             # 项目说明、API、架构与运行文档
data/                    # 真实运行数据
tests/                   # 本地隔离自动化测试与临时目录
experiment/              # 实验产物与分析记录
```

---

## 开发与验证

开发前阅读 [AGENTS.md](AGENTS.md) 和[项目总览](description/project.md)。功能变更需同步维护 `description/` 中对应专题；提示词、工具 Schema 和多轮上下文分别遵循[提示词规范](description/standard/prompt-engineering.md)与[上下文规范](description/standard/agent-context-management.md)。

当前仓库采用 Git 白名单：源码、部署资源和正式文档纳入追踪，真实 `.env`、运行数据、测试代码与实验产物不纳入追踪。因此新检出的副本可能只有测试目录占位，不能把未发现测试当作验证通过。拥有完整本地测试集时，执行：

```bash
python -m pytest -v tests/exploration_v2/ tests/assistant_v2/
```

测试必须显式覆盖数据目录：样本放在 `tests/fixtures/`，临时文件放在 `tests/tmp/` 并清理，实验产物放在 `experiment/`；不得使用默认 `data/` 写入测试数据。完整阅读入口见[文档导航](description/readme.md)。
