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

当前已实现平台会话、用户基础设施、自动执行的启动用户/管理员全表同步与首次登录用户登记，以及显式用户登录、中央 token 查询、复用认证的头像读取及平台信息查询。问答已提供内部 Agent Core、树状上下文、节点持久化与模式工厂，已接入探索问答树创建、列表、改名、完整打开与删除，已接入先注册再由 SSE 订阅激活的探索问答任务。已有专题文档：

- [提示词工程规范](standard/prompt-engineering.md)
- [多轮上下文规范](standard/agent-context-management.md)
- [实验验证规范](standard/experiment.md)
- [Python 环境与依赖](capability/application/dependencies.md)
- [Docker 构建与部署](capability/application/docker.md)
- [中间件平台注册与心跳](capability/infrastructure/middleware.md)
- [人员与权限数据契约](architecture/data/personnel.md)
- [问答智能体](architecture/workflow/question-answering/readme.md)
- [上下文渲染与消息](architecture/workflow/question-answering/context-rendering.md)
- [失败反馈与修复边界](architecture/workflow/question-answering/error-feedback.md)
- [探索任务注册与 SSE 执行](architecture/workflow/question-answering/exploration-session.md)：六项输入、订阅激活、回放、取消和删除协调。
- [多轮工作区精简](architecture/workflow/question-answering/workspace-compression.md)
- [主题发现与工作记忆总结](architecture/workflow/question-answering/topic-summary.md)
- [Agent Core 包结构](architecture/workflow/question-answering/package-structure.md)
- [助理与探索模式装配](architecture/workflow/question-answering/agent-modes.md)
- [公司背景与默认指代](architecture/workflow/question-answering/company-background.md)：公开业务背景、公司指代和资料核实边界。
- [问答树存储与内存索引](architecture/data/question-tree.md)
- [个人助理、任务与提醒存储](architecture/data/assistant-storage.md)
- [助理最近摘要记忆加载](architecture/workflow/question-answering/assistant-memory.md)
- [员工个人信息与助理私有画像](architecture/workflow/question-answering/assistant-personal-context.md)：员工事实背景、画像全文查询与 LRU 分页，以及逐条增删与事务恢复。
- [助理消息发布、领取与确认](capability/infrastructure/assistant-queue.md)
- [助理消费、消息工具与交付](capability/application/assistant-consumption.md)
- [助理最终正文转换](architecture/workflow/question-answering/assistant-message-rendering.md)
- [助理一日场景与三用户实验](capability/application/assistant-lifecycle-experiment.md)：真实队列/模型、模拟钉钉、压力指标与对话审阅。
- [助理定时任务](capability/application/assistant-reminders.md)：查看、创建、删除、到期入队和事务交付。
- [助理空闲心跳](capability/application/assistant-heartbeat.md)：中央状态判定、每用户间隔与稳定请求去重。
- [钉钉单聊机器人接入](capability/infrastructure/dingtalk-robot.md)
- [钉钉消息入站与整条校验](capability/application/assistant-inbound.md)
- [节点落盘与恢复](architecture/data/node-persistence.md)
- [用户附件引用与文件定位](architecture/data/question-file.md)
- [Core 工具注册、返回与自动折叠](architecture/workflow/question-answering/tool-content-contract.md)
- [注册中心文件图像查看](architecture/workflow/question-answering/registry-file-reading.md)
- [注册中心文件查询](architecture/workflow/question-answering/file-retrieval.md)
- [Core 子管理平台查看](architecture/workflow/question-answering/sub-management-platform.md)
- [跨平台广播与续聊](architecture/workflow/question-answering/platform-a2a.md)：两项工具、共享反馈调度、响应缓冲、私有账本与 a2a 展示状态。
- [Core 网页搜索与正文读取](architecture/workflow/question-answering/web-search.md)
- [Core 外部专家问答](architecture/workflow/question-answering/external-expert.md)
- [Core 图像文件阅读](architecture/workflow/question-answering/image-reading.md)
- [节点生命周期与用户控制](architecture/workflow/question-answering/node-lifecycle.md)
- [节点记忆向量存储](architecture/data/question-memory.md)
- [节点记忆自动向量化](capability/application/memory-embedding.md)
- [祖先任务记忆召回](architecture/workflow/question-answering/memory-retrieval.md)
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

当前目录职责如下；新增专题时在本页维护入口，具体实现状态以对应专题为准。

| 文档目录 | 收录内容 |
| --- | --- |
| `api/` | 用户与认证、问答会话和消息等对外接口契约；统一由 `api/readme.md` 汇总入口。 |
| `architecture/system/` | 身份接入、问答服务协调、组件边界和业务状态流转。 |
| `architecture/data/` | 用户身份关联、会话、消息等数据契约。 |
| `architecture/workflow/` | 本项目实际承载的 Agent 工作流及其节点、工具和上下文。 |
| `capability/application/` | 应用框架、服务装配和运行配置。 |
| `capability/infrastructure/` | 钉钉、存储和企业知识沉淀其他组件的接入说明。 |
| `standard/` | 随开发需要补充的跨模块专项规范。 |

---

## 维护约定

新增或调整专题文档时，同步更新本导航及相关入站链接。项目总览只维护定位与全局边界，专题文档维护具体设计、接口及运行说明，避免多处维护同一份规则。

应用启动及中间件联调说明见[中间件平台注册与心跳](capability/infrastructure/middleware.md)。用户与问答业务的使用步骤随相应功能实现补充。

测试和实验链接用于历史追溯。`tests/` 与 `experiment/` 的代码和产物不纳入版本追踪，部分目标在当前工作区也已缺失；保留的历史验证记录不代表当前副本可复跑，也不能代替最新功能验收。详见[版本追踪范围](project.md#版本追踪范围)。


---

## 2026-10-30 文档一致性检查

本次以当前工作区代码和配置为准，扫描 `description/` 的 60 份 Markdown，重点核对近期变更涉及的工具钩子、助理正文转换、记忆与工作区、文件授权、启动地址及运行状态。发现并修订 22 份文档：移除与实现冲突的旧状态，修正最小调用示例和两个部署 SQL 链接，补全导航，并将历史验证与当前契约分开。

验证结果：

- API 总目录的 14 个方法/路径组合与实际生成的 OpenAPI 一致。
- 最小调用示例语法、实际模式工厂构造，以及助理 7 项、探索 8 项基础工具集合校验通过；没有调用模型或数据库。
- 当前源码、配置及文档相对链接与 Markdown 标题锚点无缺失。仍有 137 处历史测试/实验引用指向本地缺失的 124 个目标；保留原记录，未补造历史产物。外部网站链接未联网验证。
- `git diff --check` 通过。本次仅修改文档，未执行完整业务回归、数据库迁移、服务重启或真实钉钉收发验收。

这些结果说明已识别的文档偏差已修正，不代表所有历史结论已复现，也不代表外部依赖当前健康。
