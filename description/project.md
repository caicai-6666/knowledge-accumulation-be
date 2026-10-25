# 企业知识沉淀客户后端

## 项目定位

`knowledge-accumulation-be` 是企业知识沉淀面向用户的客户后端，为配套前端 `knowledge-accumulation` 提供用户身份与智能体问答相关服务，并负责与企业知识沉淀的其他组件交互。

本项目围绕两个核心能力建设：管理用户的注册、登录等身份事务；管理用户的智能体问答数据，协调相关组件，为用户提供完整的问答服务。

> **当前状态：** 已实现平台会话、用户基础设施、启动用户及管理员总表同步，以及钉钉/开发显式登录、中央 token 身份查询、头像读取和平台信息查询。登录为直接装配的核心能力，用户与管理员全表启动自动同步，已通过本地隔离回归；中间件适配由用户确认，完整权威源及跨后端覆盖保障由部署侧落实；问答核心循环、树状上下文、节点持久化与模式工厂已实现，已接入探索问答树创建、列表、改名、打开与删除，已接入先注册再由 SSE 订阅激活的探索问答任务。

---

## 核心职责

### 用户与身份管理

- 为前端提供用户身份识别、登录状态及相关用户信息服务。
- 最终生产环境部署至钉钉，借助钉钉已有的用户管理能力管理本项目用户。
- 对接钉钉身份，并按业务需要维护本项目用户与钉钉身份的关联及业务数据归属。

已准备服务端开发模拟身份、钉钉 HTTP 适配、人员异步仓库和头像存储。知识后端负责用户全表注册和显式登录，生成随机用户 token，经中央成功绑定后返回；普通验证只查询已有中央登录。用户 update、权限与 SQL 管理员关系编辑、管理员增量维护保留在 registry；知识后端另承担启动管理员全表镜像，复用用户注册协调器启动自动发布，无独立启停开关。下游直接查询中间件用户 token，不再依赖知识后端 verify。

新后端对外业务前缀为 `/knowledge-accumulation/api`，registry 继续使用 `/knowledge-accumulation/registry/api`。当前提供 `auth/login`、`auth/me`、`resource/avatar/{filename}` 和 `platform/list`。平台查询聚合中间件基础信息及数据库管理员展示资料，不迁移权限或管理员增量维护。

### 智能体问答服务

- 接收用户的问答请求，关联当前用户与会话。
- 管理用户的问答会话、消息和必要的处理状态，支持前端使用这些数据。
- 与企业知识沉淀相关组件交互，协调请求、结果及失败反馈，为用户提供完整服务。
- 确保问答数据按用户及授权范围访问，避免用户间的数据混用。

已准备[问答树存储与内存索引](architecture/data/question-tree.md)：独立树表、任务/摘要节点、按需加载正文、工作区版本及最近摘要回溯。另已提供[节点落盘与恢复](architecture/data/node-persistence.md)，保存完整生成、公开消息和检查点快照。[核心 Agent 循环与上下文管理](architecture/workflow/question-answering/readme.md)已接入内部节点生成入口，支持工作区、中途与最终输出、受控摘要及私有生成落盘。另支持[用户附件登记与读取](architecture/data/question-file.md)，使用 UUID 和固定类型后缀定位磁盘文件。生成支持内部 Python 入口及[探索任务注册与 SSE](architecture/workflow/question-answering/exploration-session.md)，HTTP 可直接上传 PDF；已接入[注册中心文件查询](architecture/workflow/question-answering/file-retrieval.md)，支持名称/摘要组合检索、单个通行证精确查找、平台过滤与分页列表，已接入[注册中心PDF逐页查看](architecture/workflow/question-answering/registry-file-reading.md)，使用本轮用户Bearer远程授权读取；应用不自动执行数据库迁移。

节点记忆另提供 [MyVector/vLLM 基础设施](capability/infrastructure/myvector.md)，以权威记忆和消息表刷新独立缓存，支持 HNSW 向量及[原生中文全文检索](capability/infrastructure/fulltext.md)，按用户与树过滤、返回前回查。已接入[节点记忆自动向量化](capability/application/memory-embedding.md)，配置 Embedding 地址后后台加工终态任务的完整公开消息；已接入[祖先任务记忆召回](architecture/workflow/question-answering/memory-retrieval.md)，检索时按需刷新缓存、混合召回并分页查看，范围限于当前任务的祖先父链。插件和模型部署由运行环境显式准备。

已实现[探索问答树列表 API](api/exploration.md#获取问答树列表)，中央身份校验后返回本人 exploration 树的 ID、名称及正在执行任务的标识 `has_running_task`；助理树不返回；另提供[本人探索树改名](api/exploration.md#修改问答树名称)。[打开接口](api/exploration.md#打开问答树)已支持将完整结构及公开历史加载到内存并返回，[删除接口](api/exploration.md#删除问答树)清理关联 SQL、内存快照及已登记附件，支持失败后重试。

Core 已提供[独立图像文件阅读工具](architecture/workflow/question-answering/image-reading.md)，支持多图逐页原生图片展示与 LRU 文件模型驻留；音频和视频阅读尚未接入。

个人助理已准备[助理绑定、任务执行、交付与提醒存储](architecture/data/assistant-storage.md)：每位用户绑定唯一 assistant 树，队列附件复用现有上传目录、节点登记和 Core 查看工具。生成路径已支持[最近摘要边界的局部记忆加载](architecture/workflow/question-answering/assistant-memory.md)；另提供[中间件队列发布、领取、续租与确认](capability/infrastructure/assistant-queue.md)及单次消费处理器。已接入[后台消费、助理消息工具与可靠交付](capability/application/assistant-consumption.md)，默认关闭；已实现[查看、创建、删除定时任务及到期调度](capability/application/assistant-reminders.md)，已接入[空闲心跳调度](capability/application/assistant-heartbeat.md)，已接入[钉钉消息原子接收与发布](capability/application/assistant-inbound.md)，不支持的附件导致整条拒绝；未执行真实数据库迁移。

另已准备[钉钉单聊机器人基础设施](capability/infrastructure/dingtalk-robot.md)：Stream 接收、可信单聊过滤、OpenAPI 完整回复和主动发送、附件下载。已通过可靠接收器接入队列，与助理消费共用默认关闭的启动开关，尚未完成真实机器人联通。

[vLLM 模型基础设施](capability/infrastructure/vllm-models.md)已准备 MLLM/Embedding 配置、异步生成、工具及流式响应、原生推理返回、共享并发和服务端分词；用户已填写实际地址并通过合成样例联通验证，已供[问答 Agent Core](architecture/workflow/question-answering/readme.md)调用。

---

## 系统边界与总览流程

| 参与方 | 职责 |
| --- | --- |
| 前端 `knowledge-accumulation` | 提供面向用户的页面和交互，调用本项目服务。 |
| 本项目 `knowledge-accumulation-be` | 提供身份接入、用户问答数据管理及跨组件服务协调。 |
| 钉钉 | 提供生产环境所依托的用户管理和身份能力。 |
| 中间件 `middleware-service` | 管理平台白名单与会话，本项目以 `knowledge-accumulation` 身份登录并维持心跳。 |
| 企业知识沉淀其他组件 | 按后续约定的接口提供智能体问答所需能力。 |

规划中的主要交互如下，具体协议尚未确定：

1. 用户通过前端进入系统；生产环境结合钉钉身份完成登录与用户关联。
2. 前端携带有效身份向本项目提交问答请求。
3. 本项目校验用户及数据访问权限，管理会话与消息，并调用所需组件。
4. 本项目处理调用结果与失败状态，将用户可用的结果返回前端，并维护相应问答数据。

其他组件内部的知识采集、加工、检索或智能体执行逻辑不自动纳入本项目范围。只有明确由本项目承担的能力，才在此实现；外部能力通过适配接口接入。

---

## 全局开发原则

- 身份边界清晰：用户身份、钉钉身份和业务数据归属应有明确关联，接口必须校验访问权限。
- 数据状态可靠：明确请求、处理中、成功与失败等业务语义，避免将未完成结果当作正式答案。
- 组件职责独立：业务服务与钉钉、存储、外部问答组件的适配逻辑保持清晰边界。
- 配置以实际实现为准：已选用 FastAPI、OpenAI 异步 SDK、LangGraph 和 MySQL 异步驱动 asyncmy，环境说明见[Python 环境与依赖](capability/application/dependencies.md)；中间件接入已有配置，其他组件配置与部署方式随实现补充，不沿用其他项目的配置假设。
- 代码与文档同步：接口、数据契约、组件依赖及运行方式随实现维护，区分规划、实验和已实现能力。

---

## 当前目录与阅读入口

| 路径 | 当前用途 |
| --- | --- |
| `app/` | FastAPI 入口、平台会话、人员仓库、用户适配及启动与首次登录共用的全表注册协调器。 |
| `tests/` | 使用隔离配置及模拟外部服务的自动化测试。 |
| `data/` | 项目真实运行数据目录；头像迁移仅按用户单独授权执行。 |
| `experiment/` | 实验数据、运行产物与分析记录目录。 |
| `description/` | 项目说明、文档规范与后续专题文档。 |

开始开发前阅读[项目开发规范](../AGENTS.md)；文档组织遵循[文档撰写风格手册](documentation.md)。专题入口统一维护在[项目文档导航](readme.md)。

---

## 版本追踪范围

根目录 [.gitignore](../.gitignore) 使用白名单，只放行以下内容；新增部署资源时须同步明确其追踪规则。

| 范围 | 保留内容 |
| --- | --- |
| 部署必需文件 | `app/**/*.py`、`schema/*.sql`、`assets/tool-tag/*.txt`、`requirement.txt`、`.env.example`、`Dockerfile` 和 `.dockerignore`。 |
| 描述与维护规范 | `description/**/*.md`、`AGENTS.md` 和 `.gitignore`。 |
| 必要目录层级 | 通过 `.gitkeep` 保留 `data/user/avatar/`、`data/user/upload-file/`、`tests/fixtures/`、`tests/tmp/` 和 `experiment/`。 |

真实 `.env`、用户数据、测试代码及样本、实验脚本及产物、IDE 配置和缓存均不纳入追踪；忽略规则不会删除本地文件。文档中的测试和实验链接用于已有本地工作区的历史追溯，按此范围获取的部署副本不包含这些目标，也不能据此在部署副本复跑历史测试。实际运行配置和数据由部署环境独立提供，空目录占位不替代数据迁移。
