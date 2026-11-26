# 企业知识沉淀客户后端

## 项目定位

`knowledge-accumulation-be` 是企业知识沉淀面向用户的客户后端，为配套前端 `knowledge-accumulation-fe` 提供用户身份与智能体问答相关服务，并负责与企业知识沉淀的其他组件交互。

本项目围绕两个核心能力建设：管理用户的注册、登录等身份事务；管理用户的智能体问答数据，协调相关组件，为用户提供完整的问答服务。

> **当前状态：** 已实现平台会话、用户基础设施、启动用户及管理员总表同步，以及钉钉/开发显式登录、中央 token 身份查询、头像读取和平台信息查询。探索模式（Exploration）与个人助理（Assistant）均已全面切换至独立库 `agent-core` 与外部 MCP 服务（`knowledge-accumulation-mcp-server`），历史自研 `app/agent/core/` 与本地 `file_conversion` 已彻底移除。当前 33 项自动化测试全部通过，独立隔离容器已完成 Exploration 全链路真实联调（认证、树管理、主线/分支 SSE、MCP 工具、取消与删树）；个人助理（1B 内置提醒/心跳 + 2A 个人文件备忘 + 钉钉正文转换）已通过自动化隔离验证，尚未执行真实钉钉单聊线上收发验收。

---

## 核心职责

### 用户与身份管理

- 为前端提供用户身份识别、登录状态及相关用户信息服务。
- 最终生产环境部署至钉钉，借助钉钉已有的用户管理能力管理本项目用户。
- 对接钉钉身份，并按业务需要维护本项目用户与钉钉身份的关联及业务数据归属。

已准备服务端开发模拟身份、钉钉 HTTP 适配、人员异步仓库和头像存储。知识后端负责用户全表注册和显式登录，生成随机用户 token，经中央成功绑定后返回；普通验证只查询已有中央登录。用户 update、权限与 SQL 管理员关系编辑、管理员增量维护保留在 registry；知识后端另承担启动管理员全表镜像，复用用户注册协调器启动自动发布，无独立启停开关。下游直接查询中间件用户 token，不再依赖知识后端 verify。

对外业务前缀为 `/knowledge-accumulation/api`，registry 继续使用 `/knowledge-accumulation/registry/api`。身份与平台相关接口包括 `auth/login`、`auth/me`、`resource/avatar/{filename}`、`resource/attachment/{tree_id}/{file_id}`、`employee/avatar/fill` 和 `platform/list`。

### 智能体问答服务

- 接收用户的问答请求，关联当前用户与会话。
- 管理用户的问答会话、消息和必要的处理状态，支持前端使用这些数据。
- 与企业知识沉淀相关组件交互，协调请求、结果及失败反馈，为用户提供完整服务。
- 确保问答数据按用户及授权范围访问，避免用户间的数据混用。

智能体底层能力统一由独立库 `agent-core`（见[独立 Agent Core 库接入](capability/application/agent-core-library.md)）和外部[MCP 工具服务](capability/infrastructure/mcp-server.md)提供：

- **探索模式（Exploration）**：基于 `agent_core.AgentRuntime` 与 `TreeAgent(execution_mode='interruptible', workspace_retention='per_node')`，由[探索树与 agent-core 存储契约](architecture/data/question-tree.md)维护 `exploration_trees`（含真实主干末尾 `main_leaf_id`）与 `exploration_tree_files`；支持[探索问答树 API](api/exploration.md)全部 9 个端点（建树、列表、改名、全量打开、删树，以及先注册再由 SSE 订阅激活的[探索任务与 SSE 执行](architecture/workflow/question-answering/exploration-session.md)、历史节点持久化 `branch` 分叉、取消与改向）。附件经 MCP `file_conversion` 统一转 PDF 并写入 `UploadFileStore`（见[用户附件引用与文件定位](architecture/data/question-file.md)）。
- **个人助理（Assistant）**：基于 `agent_core.AgentRuntime` 与 `TreeAgent(execution_mode='queue', workspace_retention='latest')`，保留中间件队列消费（`GET_LOCK` 跨实例用户锁、租约与确认，见[助理消费、运行时与可靠交付](capability/application/assistant-consumption.md)）、[钉钉单聊机器人接入](capability/infrastructure/dingtalk-robot.md)与[钉钉消息入站与附件转换](capability/application/assistant-inbound.md)（附件经 MCP `file_conversion` 转 PDF）。采用 **1B 方案**使用 `agent-core` 内置[定时提醒](capability/application/assistant-reminders.md)与[空闲心跳及原生裁剪](capability/application/assistant-heartbeat.md)，采用 **2A 方案**保留 BE 独立[上传文件元信息与个人备忘](architecture/data/assistant-file-notes.md)，保留[私有画像](architecture/workflow/question-answering/assistant-personal-context.md)、[钉钉日程集查询](capability/application/assistant-calendar.md)、[文件内容告知检查](architecture/workflow/question-answering/file-disclosure.md)及[助理最终正文转换](architecture/workflow/question-answering/assistant-message-rendering.md)。
- **共享工具与模型**：复用[vLLM 模型配置与异步客户端](capability/infrastructure/vllm-models.md)、[注册中心文件查询](architecture/workflow/question-answering/file-retrieval.md)、[双源 PDF 文件查看](architecture/workflow/question-answering/registry-file-reading.md)、[子管理平台查看](architecture/workflow/question-answering/sub-management-platform.md)、[跨平台广播与续聊](architecture/workflow/question-answering/platform-a2a.md)及 7 个 MCP 外部工具。

---

## 系统边界与总览流程

| 参与方 | 职责 |
| --- | --- |
| 前端 `knowledge-accumulation-fe` | 提供面向用户的页面和交互，调用本项目服务。 |
| 本项目 `knowledge-accumulation-be` | 提供身份接入、用户问答数据管理及跨组件服务协调。 |
| 钉钉 | 提供生产环境所依托的用户管理和身份能力。 |
| 中间件 `middleware-service` | 管理平台白名单、用户会话与助理任务队列，本项目以 `knowledge-accumulation` 身份登录并维持心跳。 |
| 外部 MCP 服务 `knowledge-accumulation-mcp-server` | 提供时间/日期、网页搜索/读取、外部专家问答及 `file_conversion`（文档/图片转 PDF）。 |
| 企业知识沉淀其他组件 | 按约定接口提供注册中心文件检索、远程 PDF 读取与跨平台 A2A 协作。 |

当前前端问答交互如下，具体协议见[API 接口导航](api/readme.md)：

1. 用户通过前端进入系统；生产环境结合钉钉身份完成登录与用户关联。
2. 前端携带有效身份向本项目提交问答请求。
3. 本项目校验用户及数据访问权限，管理会话与消息，并调用 `agent-core` 与外部组件。
4. 本项目处理调用结果与失败状态，将用户可用的结果返回前端，并维护相应问答数据。

---

## 全局开发原则

- 身份边界清晰：用户身份、钉钉身份和业务数据归属应有明确关联，接口必须校验访问权限。
- 数据状态可靠：明确请求、处理中、成功与失败等业务语义，避免将未完成结果当作正式答案。
- 组件职责独立：业务服务与钉钉、存储、外部 MCP 及模型组件的适配逻辑保持清晰边界。
- 配置以实际实现为准：选用 FastAPI、`agent-core[mysql,pdf]`、`mcp>=2.3.0`、OpenAI 异步 SDK 与 MySQL 异步驱动 `asyncmy`，环境说明见[Python 环境与依赖](capability/application/dependencies.md)。
- 代码与文档同步：接口、数据契约、组件依赖及运行方式随实现维护，区分规划、已验证与未线上验证能力。

---

## 当前目录与阅读入口

| 路径 | 当前用途 |
| --- | --- |
| `app/` | FastAPI 入口、探索与助理工具装配、业务服务、数据仓库及外部基础设施适配。 |
| `tests/` | 使用隔离配置及模拟外部服务的自动化测试。 |
| `data/` | 项目真实运行数据目录；测试与实验禁止写入此目录。 |
| `experiment/` | 实验数据、运行产物与分析记录目录。 |
| `description/` | 项目说明、文档规范与专题文档。 |

开始开发前阅读[项目开发规范](../AGENTS.md)；文档组织遵循[文档撰写风格手册](documentation.md)。专题入口统一维护在[项目文档导航](readme.md)。

---

## 版本追踪范围

根目录 [.gitignore](../.gitignore) 使用白名单，只放行以下内容；新增部署资源时须同步明确其追踪规则。

| 范围 | 保留内容 |
| --- | --- |
| 部署必需文件 | `app/**/*.py`、`requirement.txt`、`.env.example`、`Dockerfile` 和 `.dockerignore`。 |
| 描述与维护规范 | `README.md`、`description/**/*.md`、`AGENTS.md` 和 `.gitignore`。 |
| 必要目录层级 | 通过 `.gitkeep` 保留 `data/user/avatar/`、`data/user/upload-file/`、`tests/fixtures/`、`tests/tmp/` 和 `experiment/`。 |

真实 `.env`、用户数据、测试代码及样本、实验脚本及产物、IDE 配置和缓存均不纳入追踪；实际运行配置和数据由部署环境独立提供，应用启动不自动执行数据库迁移。
