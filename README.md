# 企业知识沉淀客户后端

`knowledge-accumulation-be` 为企业知识沉淀前端 `knowledge-accumulation-fe` 提供身份认证、探索问答和个人助理服务。项目基于 Python 3.12、FastAPI、异步 Agent Core 和 MySQL，通过中间件连接平台会话、任务队列及跨平台协作能力，并通过钉钉机器人提供员工个人助理入口。

[项目总览](description/project.md) · [文档导航](description/readme.md) · [API 参考](description/api/readme.md) · [开发规范](AGENTS.md)

---

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 用户与身份 | 钉钉登录、开发登录、中央会话验证、员工及管理员全表同步、头像与平台信息查询。 |
| 探索问答 | 问答树管理、历史节点分支、PDF 上传、任务注册、SSE 输出、取消与公开历史恢复。 |
| 个人助理 | 钉钉单聊接收、队列消费、员工背景、私有画像、助理间消息、定时任务与空闲心跳。 |
| 资料与检索 | 注册中心文件查询及授权阅读、图像逐页查看、网页搜索与正文读取、专家问答、祖先记忆召回。 |
| 上下文与持久化 | 工作区、受控压缩、任务检查点、公开消息和私有生成审计；助理维护线性记忆窗口与当前工作区。 |
| 平台协作 | 通过中间件 A2A 队列广播问题、接收反馈和继续交流，可按配置启用。 |

探索与个人助理共用 Core，由模式工厂装配各自提示词和工具。每个工具独立注册执行后钩子，并在权威提交前顺序执行。个人助理的 `finish_task` 默认通过单次转换子智能体生成适合钉钉的 Markdown 正文；转换结果随后写入最终答案和已接受工具历史，供交付及下次上下文使用。详细契约见[助理正文转换](description/architecture/workflow/question-answering/assistant-message-rendering.md)。

---

## 快速开始

### 1. 准备环境

使用 Python 3.12，在项目根目录创建隔离环境并安装固定依赖。以下为 macOS / Linux 示例：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirement.txt
python -m pip check
```

也可使用 Conda，见 [Python 环境与依赖](description/capability/application/dependencies.md)。

### 2. 配置依赖

首次运行时从模板创建 `.env`；已有文件则保留：

```bash
if [ ! -e .env ]; then
    cp .env.example .env
fi
```

编辑 `.env`，替换地址、账号和凭据占位值。应用通常从项目根目录加载该文件，进程环境变量优先，不展开 `${...}`。完整参数见 [.env.example](.env.example)，首次接入重点配置：

| 配置 | 用途与要求 |
| --- | --- |
| `MIDDLEWARE_BASE_URL`、`MIDDLEWARE_SECRET` | 中间件地址及本平台密钥；平台代码使用 `knowledge-accumulation`。登录和助理队列依赖该会话。 |
| `APP_ENV`、`AUTH_DEVELOPER_ACCESS_TOKEN` | 本地登录使用 `development` 和非空固定登录凭据；该凭据用于换取中央用户 token。 |
| `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD` | 配置已准备的数据库，密码不能为空；人员数据须与 registry 使用同一权威源。 |
| `VLLM_MLLM_BASE_URL`、`VLLM_MLLM_MODEL` | 问答模型地址与实际服务模型名；按服务配置 API key 和工具格式模板。 |
| `VLLM_EMBEDDING_BASE_URL` | 向量模型地址；留空时不启用后台记忆向量化，配置后启动扫描已落盘消息。 |
| `REGISTRY_BASE_URL`、`FILE_SOURCE_MYSQL_*` | 注册中心原件服务与文件检索数据源，按需配置。 |
| `ASSISTANT_ENABLED`、`PLATFORM_A2A_ENABLED` | 默认均为 `false`，完成对应依赖和迁移后再开启。 |

主模型服务需支持原生工具调用和 `/tokenize`，主题总结还使用 JSON Schema；助理正文转换通过提示生成 JSON 并在本地使用 Pydantic 校验；模型名、上下文容量和工具模板需与实际服务一致，见[模型配置](description/capability/infrastructure/vllm-models.md)。仅启动身份接口且关闭助理时，主模型地址可以暂留空。

中间件接入后会自动同步用户及管理员全表，启动前应准备完整权威人员源。数据库结构由部署侧显式初始化或迁移，应用不会自动建表；最新初始化入口与已有数据库边界见[数据与迁移](description/capability/application/docker.md#数据与迁移)。不要将全部 SQL 无差别重复执行到已有数据库。

如只查看 Web 入口和 OpenAPI，可同时清空中间件地址及密钥，保持助理、A2A 关闭并留空 Embedding 地址；这不提供可用的登录和完整问答服务。

### 3. 启动本地服务

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

OpenAPI 可访问仅说明 Web 入口正常；登录、数据库和模型仍需分别验证。开发登录流程见[认证 API](description/api/auth.md)，探索任务的注册、订阅激活和取消顺序见[探索问答 API](description/api/exploration.md)。

---

## 启用钉钉个人助理

准备中间件队列、已迁移的助理存储及可用模型服务后，配置 `DINGTALK_CORP_ID`、`DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET` 和 `DINGTALK_ROBOT_CODE`，并设置 `ASSISTANT_ENABLED=true`。应用会同时装配 Stream 入站、任务消费、提醒及空闲心跳调度。生产登录另需设置 `APP_ENV=production`。

钉钉后台需完成机器人发布、可见范围及权限配置，具体步骤见[机器人接入](description/capability/infrastructure/dingtalk-robot.md)。新空库统一使用 deploy 的 `mysql/init/001-schema.sql`，不需要历史迁移或工作区数据迁移器。

最终正文转换使用同一 MLLM 服务，默认开启 `medium` 原生推理（`ASSISTANT_RENDERING_THINKING_EFFORT=medium`），通过提示生成 JSON 并由本地 Pydantic 严格校验，不发送接口级 `response_format`；该配置与主助理推理档位独立。转换失败不会自动降级发送原文；任务失败、确认与交付恢复的当前规则见[助理消费](description/capability/application/assistant-consumption.md#失败与恢复)。

---

## Docker 部署

项目提供 [Dockerfile](Dockerfile)，可在根目录构建：

```bash
docker build -t knowledge-accumulation-be:dev .
```

镜像使用 Python 3.12，以非 root 用户运行，容器内部监听 `8000`，固定单 worker。统一 Compose 编排维护在 `knowledge-accumulation-deploy` 项目；正式部署时本项目位于其 `knowledge-accumulation-be/` 子目录，默认暴露宿主端口 `24000`，业务地址为 `http://127.0.0.1:24000/knowledge-accumulation/api`。

Docker / ASGI 导入默认不带 `/dev`；`APP_PATH_PREFIX` 通过进程环境在路由导入前设置。实际 `.env` 通过只读挂载提供，运行数据使用持久卷。镜像不包含本机凭据和用户数据，也不会自动执行迁移。部署步骤、挂载权限、环境覆盖及 SSE 代理要求统一见[Docker 构建与部署](description/capability/application/docker.md)。

---

## 目录结构

```text
app/
├── main.py              # 应用入口与生命周期装配
├── agent/               # Core、探索模式、个人助理及内部子智能体
├── auth/                # 身份认证适配
├── config/              # 配置加载与校验
├── infrastructure/      # 模型、中间件、钉钉等外部系统适配
├── model/               # 业务数据模型
├── repository/          # SQL 持久化与查询
├── router/              # HTTP 与 SSE 路由
├── schema/              # API 请求与响应模型
└── service/             # 业务编排、后台任务与交付
assets/tool-tag/         # 模型工具格式模板
description/             # 项目说明、API、架构与运行文档
data/                    # 真实运行数据
tests/                   # 本地测试、样本与临时文件
experiment/              # 实验产物与分析记录
```

---

## 开发与验证

开发前阅读 [AGENTS.md](AGENTS.md) 和[项目总览](description/project.md)。功能变更需同步维护 `description/` 中对应专题；提示词、工具 Schema 和多轮上下文分别遵循[提示词规范](description/standard/prompt-engineering.md)与[上下文规范](description/standard/agent-context-management.md)。

当前仓库采用 Git 白名单：源码、部署资源和正式文档纳入追踪，真实 `.env`、运行数据、测试代码与实验产物不纳入追踪。因此新检出的副本可能只有测试目录占位，不能把未发现测试当作验证通过。拥有完整本地测试集时，按各专题准备隔离依赖后执行：

```bash
python -m unittest discover -s tests
```

测试必须显式覆盖数据目录：样本放在 `tests/fixtures/`，临时文件放在 `tests/tmp/` 并清理，实验产物放在 `experiment/`；不得使用默认 `data/` 写入测试数据。需要临时 MySQL / MyVector 的用例按专题配置，跳过的集成测试不代表已验证。完整阅读入口见[文档导航](description/readme.md)。
