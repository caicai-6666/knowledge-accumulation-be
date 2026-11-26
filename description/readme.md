# 项目文档导航

本目录维护企业知识沉淀客户后端 `knowledge-accumulation-be` 的项目说明、开发规范及功能文档。配套前端项目为 `knowledge-accumulation-fe`。

---

## 从哪里开始

| 文档 | 阅读目的 |
| --- | --- |
| [仓库首页](../README.md) | 快速了解主要能力、首次配置、本地启动和部署入口。 |
| [项目总览](project.md) | 了解项目定位、两项核心职责、系统边界和当前实现状态。 |
| [项目开发规范](../AGENTS.md) | 开发前必读，了解代码、提示词、文档同步和实验产物要求。 |
| [文档撰写风格手册](documentation.md) | 新建或修改文档前，确认目录、命名、格式和链接规则。 |

---

## 专题文档归档

### 开发规范（`standard/`）

- [提示词工程规范](standard/prompt-engineering.md)
- [工具反馈渲染规范](standard/tool-feedback.md)：结构化结果与模型正文分离，统一采用 `agent_core.FeedbackOnlyHook` 渲染面向模型的中文正文。
- [多轮上下文规范](standard/agent-context-management.md)
- [实验验证规范](standard/experiment.md)

### 对外接口契约（`api/`）

- [API 接口导航](api/readme.md)：汇总全部 14 个对外 HTTP / SSE 端点。
- [用户认证 API](api/auth.md)
- [用户资源 API](api/resource.md)：头像读取与本人探索树附件读取。
- [员工头像补齐 API](api/employee-avatar.md)
- [平台信息 API](api/platform.md)
- [探索问答树 API](api/exploration.md)：探索树 CRUD、打开与任务/SSE 全部 9 个端点。

### 系统与数据契约（`architecture/system/`、`architecture/data/`）

- [登录与会话管理](architecture/system/user-auth.md)
- [人员与权限数据契约](architecture/data/personnel.md)
- [探索树与 agent-core 存储契约](architecture/data/question-tree.md)：`agent-core` 5 张核心表与 `exploration_trees`（含 `main_leaf_id`）/ `exploration_tree_files` 业务表。
- [用户附件引用与文件定位](architecture/data/question-file.md)：`AttachmentReference`、`UploadFileStore` 与跨模式读取授权。
- [个人助理存储契约](architecture/data/assistant-storage.md)：`MySQLRepositoryBase`、`assistants`、`assistant_task_executions`、`assistant_deliveries`、`assistant_tree_files` 与画像/入站账本。
- [上传文件元信息与个人备忘](architecture/data/assistant-file-notes.md)：2A 方案保留 `uploaded_files` 与 `assistant_file_notes` 表及全部工具。

### 问答与助理工作流（`architecture/workflow/question-answering/`）

- [问答智能体总览](architecture/workflow/question-answering/readme.md)
- [探索与助理模式装配](architecture/workflow/question-answering/agent-modes.md)
- [探索任务注册与 SSE 执行](architecture/workflow/question-answering/exploration-session.md)：注册、订阅激活、流式预览、回放、取消、改向与分支。
- [公司背景与默认指代](architecture/workflow/question-answering/company-background.md)
- [工具内容与折叠契约](architecture/workflow/question-answering/tool-content-contract.md)
- [工具失败处理与诚实反馈](architecture/workflow/question-answering/tool-failures.md)
- [注册中心文件查询](architecture/workflow/question-answering/file-retrieval.md)
- [双源 PDF 文件查看](architecture/workflow/question-answering/registry-file-reading.md)
- [子管理平台查看](architecture/workflow/question-answering/sub-management-platform.md)
- [跨平台广播与续聊](architecture/workflow/question-answering/platform-a2a.md)
- [个人助理记忆与工作区管理](architecture/workflow/question-answering/assistant-memory.md)
- [员工个人信息与助理私有画像](architecture/workflow/question-answering/assistant-personal-context.md)
- [助理最终正文转换](architecture/workflow/question-answering/assistant-message-rendering.md)
- [文件内容告知检查](architecture/workflow/question-answering/file-disclosure.md)

### 应用能力（`capability/application/`）

- [独立 Agent Core 库接入](capability/application/agent-core-library.md)
- [Python 环境与依赖](capability/application/dependencies.md)
- [Docker 构建与部署](capability/application/docker.md)
- [附件分类转换为 PDF](capability/application/attachment-conversion.md)
- [用户全表注册协调](capability/application/startup-user-sync.md)
- [启动平台管理员全表同步](capability/application/startup-admin-sync.md)
- [助理消费、运行时与可靠交付](capability/application/assistant-consumption.md)
- [助理定时提醒（1B agent-core 内置方案）](capability/application/assistant-reminders.md)
- [助理空闲心跳（1B agent-core 原生方案）](capability/application/assistant-heartbeat.md)
- [助理日程集查询](capability/application/assistant-calendar.md)
- [钉钉消息入站与附件转换](capability/application/assistant-inbound.md)

### 基础设施能力（`capability/infrastructure/`）

- [外部 MCP 工具服务接入](capability/infrastructure/mcp-server.md)
- [vLLM 模型配置与异步客户端](capability/infrastructure/vllm-models.md)
- [中间件平台注册与心跳](capability/infrastructure/middleware.md)
- [用户基础设施](capability/infrastructure/user-infrastructure.md)
- [助理消息发布、领取与确认](capability/infrastructure/assistant-queue.md)
- [钉钉单聊机器人接入](capability/infrastructure/dingtalk-robot.md)

---

## 目录职责与维护约定

| 文档目录 | 收录内容 |
| --- | --- |
| `api/` | 用户与认证、资源、平台与探索问答树对外接口契约；统一由 `api/readme.md` 汇总入口。 |
| `architecture/system/` | 身份接入、服务协调、组件边界和业务状态流转。 |
| `architecture/data/` | 用户身份关联、`agent-core` 核心表及探索/助理业务表数据契约。 |
| `architecture/workflow/` | 本项目承载的 Agent 工作流、工具契约、提示词与上下文。 |
| `capability/application/` | 应用框架、运行装配、依赖与部署配置。 |
| `capability/infrastructure/` | 外部 MCP 服务、vLLM、钉钉、中间件等基础设施适配说明。 |
| `standard/` | 跨模块专项开发与渲染规范。 |

新增或调整专题文档时，同步更新本导航及相关入站链接。项目总览只维护定位与全局边界，专题文档维护具体设计、接口及运行说明，避免多处维护同一份规则。
