# 项目文档导航

本目录维护企业知识沉淀客户后端 `knowledge-accumulation-be` 的项目说明、开发规范及后续功能文档。配套前端项目为 `knowledge-accumulation`。

---

## 从哪里开始

| 文档 | 阅读目的 |
| --- | --- |
| [项目总览](project.md) | 了解项目定位、两项核心职责、系统边界和当前实现状态。 |
| [项目开发规范](../AGENTS.md) | 开发前必读，了解代码、提示词、文档同步和实验产物要求。 |
| [文档撰写风格手册](documentation.md) | 新建或修改文档前，确认目录、命名、格式和链接规则。 |

---

## 专题文档归档

当前已实现平台会话、用户基础设施、自动执行的启动用户/管理员全表同步与首次登录用户登记，以及显式用户登录、中央 token 查询、复用认证的头像读取及平台信息查询。问答已提供内部 Agent Core、树状上下文、节点持久化与模式工厂，已接入探索问答树创建、列表、改名、完整打开与删除，已接入先注册再由 SSE 订阅激活的探索问答任务。已有专题文档：

- [提示词工程规范](standard/prompt-engineering.md)
- [多轮上下文规范](standard/agent-context-management.md)
- [实验验证规范](standard/experiment.md)
- [Python 环境与依赖](capability/application/dependencies.md)
- [Docker 构建与部署](capability/application/docker.md)
- [中间件平台注册与心跳](capability/infrastructure/middleware.md)
- [人员与权限数据契约](architecture/data/personnel.md)
- [问答智能体](architecture/workflow/question-answering/readme.md)
- [Agent Core 包结构](architecture/workflow/question-answering/package-structure.md)
- [助理与探索模式装配](architecture/workflow/question-answering/agent-modes.md)
- [问答树存储与内存索引](architecture/data/question-tree.md)
- [节点落盘与恢复](architecture/data/node-persistence.md)
- [用户附件引用与文件定位](architecture/data/question-file.md)
- [Core 工具注册、返回与自动折叠](architecture/workflow/question-answering/tool-content-contract.md)
- [Core 网页搜索与正文读取](architecture/workflow/question-answering/web-search.md)
- [Core 外部专家问答](architecture/workflow/question-answering/external-expert.md)
- [Core 图像文件阅读](architecture/workflow/question-answering/image-reading.md)
- [节点生命周期与用户控制](architecture/workflow/question-answering/node-lifecycle.md)
- [节点记忆向量存储](architecture/data/question-memory.md)
- [节点记忆全文检索](capability/infrastructure/fulltext.md)
- [节点记忆的 MyVector 检索](capability/infrastructure/myvector.md)
- [vLLM 模型配置与异步客户端](capability/infrastructure/vllm-models.md)
- [可选工具格式注入](capability/infrastructure/tool-tag.md)
- [用户基础设施](capability/infrastructure/user-infrastructure.md)
- [用户全表注册协调](capability/application/startup-user-sync.md)
- [启动平台管理员全表同步](capability/application/startup-admin-sync.md)
- [API 接口导航](api/readme.md)
- [用户认证 API](api/auth.md)
- [用户资源 API](api/resource.md)
- [平台信息 API](api/platform.md)
- [探索问答树 API](api/exploration.md)
- [登录与会话管理](architecture/system/user-auth.md)

后续按实际开发需要建立以下目录，并在本页添加具体文档链接；目录划分不代表功能已经实现。

| 文档目录 | 收录内容 |
| --- | --- |
| `api/` | 用户与认证、问答会话和消息等对外接口契约；有接口文档后建立 `api/readme.md` 汇总入口。 |
| `architecture/system/` | 身份接入、问答服务协调、组件边界和业务状态流转。 |
| `architecture/data/` | 用户身份关联、会话、消息等数据契约。 |
| `architecture/workflow/` | 本项目实际承载的 Agent 工作流；仅在需要时建立。 |
| `capability/application/` | 应用框架、服务装配和运行配置。 |
| `capability/infrastructure/` | 钉钉、存储和企业知识沉淀其他组件的接入说明。 |
| `standard/` | 随开发需要补充的跨模块专项规范。 |

---

## 维护约定

新增或调整专题文档时，同步更新本导航及相关入站链接。项目总览只维护定位与全局边界，专题文档维护具体设计、接口及运行说明，避免多处维护同一份规则。

应用启动及中间件联调说明见[中间件平台注册与心跳](capability/infrastructure/middleware.md)。用户与问答业务的使用步骤随相应功能实现补充。

- [探索任务注册与 SSE 执行](architecture/workflow/question-answering/exploration-session.md)：六项输入、订阅激活、回放、取消和删除协调。
