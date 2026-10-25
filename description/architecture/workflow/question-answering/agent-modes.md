# 助理与探索模式装配

## 用途与边界

个人助理是复杂任务探索的简化使用方式，两者共用 Agent Core、工作区、预算、压缩、问答树和节点持久化。业务差异放在模式包内，Core 不判断 `assistant` / `exploration`。

当前已实现模式提示词、工具组合、上下文资料契约与内部服务工厂；默认提供七个[基本工具](agent-core.md#必要工具)。助理工厂可注入已有 `memory_backend`，复用祖先记忆检索及查看；[定时任务工具](../../../capability/application/assistant-reminders.md)已实现；个人资料和外部日程系统尚未接入。助理生成使用[最近摘要边界的局部上下文](assistant-memory.md)。模式装配本身不新增 HTTP 或 SSE。另已准备[助理、任务与提醒存储](../../data/assistant-storage.md)及[队列单次消费基础设施](../../../capability/infrastructure/assistant-queue.md)，[助理消费与交付](../../../capability/application/assistant-consumption.md)已实现，默认关闭；提醒调度和[空闲心跳调度](../../../capability/application/assistant-heartbeat.md)均已接入。

---

## 包结构与职责

```text
app/agent/
├── core/                        # 通用循环、基本工具、预算、整理、提交
├── context.py                   # 树公开路径到 ContextInjection 的共用投影
├── assistant/
│   ├── __init__.py               # 显式导出工厂及资料契约
│   ├── factory.py                # 助理 Core 与持久化服务装配
│   ├── context.py                # 个人背景、偏好、相关记忆的本轮注入
│   ├── prompt/
│   │   ├── __init__.py           # 组合通用交互、工作区和助理角色
│   │   ├── role.py               # 个人工作辅助规则
│   │   └── task.py               # 四类来源任务与通知选择
│   └── tool/
│       ├── __init__.py           # 基本工具与助理工具组合入口
│       ├── finish_task.py        # 继承最终输出契约，增加条件通知
│       ├── send_assistant_message.py # 独立助理间文字通信工具
│       ├── search_assistants.py   # 按姓名关键词建立独立查询结果集
│       ├── view_assistant_query.py # 按查询 ID 和页码查看 userid/用户名
│       └── reminder.py           # 当前助理的查看、创建、删除提醒
└── exploration/
    ├── __init__.py
    ├── factory.py                # 探索 Core 与持久化服务装配
    ├── context.py                # 探索上下文构建入口
    ├── progress_reminder.py      # 每次任务的开场与周期沟通策略
    ├── output_stream.py          # SSE 正文预览、完成与中断适配
    ├── prompt/
    │   ├── __init__.py
    │   ├── role.py               # 多方向分析与证据核实规则
    │   ├── interaction.py        # 探索中途输出规则
    │   └── guidance.py           # 开场和长执行提示文案
    └── tool/
        ├── __init__.py           # 基本工具与探索工具组合入口
        ├── emit_progress.py      # emit_progress 参数与注册
        └── finish_task.py        # 继承最终输出契约，交付探索最终答复
```

未来实际实现资料或记忆工具时，在对应 `tool/` 下按业务能力增加模块，由 `build_registrations()` 导出；助理的普通扩展集合仍为空，两个模式分别继承 Core 抽象最终输出契约，并在组合点显式传入具体实例；助理扩展 send_to_user；消费宿主按任务注入 search_assistants、view_assistant_query、send_assistant_message 及三个提醒工具，探索返回 `emit_progress` 注册项。跨模式复用的业务能力确定后再抽取，数据库和外部服务访问仍放在现有 repository / infrastructure / service 中，模型工具只是适配入口。

导入上述包不加载 `.env`，不创建 HTTP 客户端、数据库连接或后台任务。模式工厂在未传 options 时读取[文件与页面运行配置](image-reading.md#运行配置)，构造 Core 不立即打开连接；如启用 [tool-tag](../../../capability/infrastructure/tool-tag.md)，仅此时加载所选静态格式文件，两模式复用同一加载规则。外部传入的模型客户端由宿主管理，自建客户端沿用 Core 的关闭规则。

---

## 工厂与调用

两个模式均导出：

| 接口 | 行为 |
| --- | --- |
| `build_core(settings, client=..., counter=..., options=..., tools=...)` | 返回配置了该模式角色及工具集合的通用 Core，适合内存宿主。 |
| `build_service(nodes, settings, ..., tools=..., tool_loader=...)` | 返回复用现有节点仓库的 QuestionAgentService，绑定对应树用途。 |

两种模式的 `build_service` 均可通过 `platform_directory` 注入只读平台目录，按任务装配[子管理平台查看工具](sub-management-platform.md)。助理 `build_service` 另接收 `context_loader`。两类 loader 均接受本次可信 `AuthenticatedUser` 的深拷贝，支持同步返回或异步返回：

- `tool_loader(user)` 返回已经按当前身份和权限筛选的 RegisteredTool 序列。回调中可将可信身份绑定到执行器闭包，不接受模型传来的 userid 作为授权依据。
- `context_loader(user)` 返回 AssistantContext 或符合该契约的字典。每次运行重新加载，不跨用户缓存个人背景。

`tools` 是宿主提供的固定注册项；只适合不绑定具体用户身份的工具。身份相关工具使用 `tool_loader`，服务为本轮创建独立工具集合，不修改共享 Core。重复工具名（包含与基本工具重名）明确拒绝，不能覆盖基本交互协议。实际业务读取仍需执行其授权校验，工具是否可见不代替资源鉴权。

```python
from app.agent.assistant import build_service

# nodes、settings、client 由现有宿主提供；两个 loader 为可信业务适配器。
service = build_service(
    nodes, settings, client=client,
    context_loader=load_personal_context,
    tool_loader=load_authorized_tools,
)
result = await service.generate(
    user=authenticated_user, tree_id=assistant_tree_id,
    question="请结合我提供的背景整理下一步工作。",
    context_injection="本次补充资料。", thinking_effort="medium",
)
```

助理不接收节点选择，自动从主干末端继续；探索 `build_service` 对非空树要求 `selected_node_id`。树用途不匹配时，在调用 loader、模型和创建任务前失败。直接 `AgentCore(...)` 仍可使用通用角色，但必须显式注入含模式 finish_task 的完整注册表，不再自动提供默认最终输出；`QuestionAgentService(nodes, core)` 继续复用调用方已经装配的 Core。工具分类与示例见[包结构](package-structure.md#工具分工与依赖)。

---

## 上下文与提示词

共用 `build_tree_context` 只投影当前父链的摘要、公开问答、中途输出及对应工作区，不加载其他分支或原生推理。模式构建器在该结果上附加资料，不另造树加载机制。

AssistantContext 分为 `profile`、`preferences`、`relevant_memories`；每个 ContextMaterial 包含 `content`、`source`、`status`，默认 `unverified`。它们不包含权限覆盖字段，用户陈述不能自动变成已确认事实。

个人资料与内部调用方的当前资料通过 `ContextInjection.supplemental` 保存，主循环在“上下文资料”文本分区作为普通 user 数据展示，保留来源和确认状态，不提升为 system/tool 指令；不自动登记记忆，也不自动修改权威工作区。空个人资料保持原始输入不变。任务前摘要完成后仍保留本轮个人资料，不因重建 ContextInjection 丢失。该内部扩展点不是探索 HTTP 的额外用户输入字段。

模式角色复用通用知识助手的事实、权限与服务边界，再加入个人辅助或复杂任务分析规则。通用交互与四区域工作区定义继续只维护一份。资料缺失、冲突或工具不可用时明确限制，不承诺未接入能力；角色文本不含树类型、内部节点、工作流拓扑或运行标识。

Core 接收有效 `system_prompt`、ToolRegistry 及可选请求提示工厂。任务前计数、固定预算、主循环消息与生成共用 `core.build_messages` / `core.fixed_messages` 及同一工具集合；本轮按用户装配的局部 Core 保持模型配置和运行选项一致。整理子 Agent 使用独立的主题规划工具、JSON Schema 总结及 工作区增删改与 finish_compression，不能调用模式业务工具；契约见[主题发现与总结](topic-summary.md)。

---

## 工具扩展与提交边界

RegisteredTool 仍绑定名称、描述、参数模型、纯预演 handler 和可选 parser，新增可选 `executor(arguments)`。handler 只在工作区副本上预演，不访问外部系统或修改共享状态；executor 在参数、状态和候选工作区验收后读取资料，支持异步返回。

所有实际参数 Schema 的属性说明继续递归校验。执行器统一返回 `ToolExecutionResult`，不再接受任意 JSON 作为成功结果；`ToolExecution` 只用于携带提交后的本地回调。注册必须声明返回内容类别，可折叠工具还需绑定页面解析器，完整定义见[公共工具契约](tool-content-contract.md)。普通读取可返回：

```python
ToolExecutionResult(status='succeeded', tool_result={
    'status': 'accepted', 'result': {'text': '资料内容', 'source': '业务来源'},
})
```

结果随 assistant/tool 对及工作区成功提交后进入下一轮上下文。`failed` 反馈只存在于连续纠错轨迹；成功后整段清除。`unknown`、异常、取消、非法结果或提交失败停止，不伪造成功或自动重放。原始生成完整保存在私有记录中。

当前 executor 入口用于资料读取。外部写操作与本地节点事务不具备原子保证，本轮不接入此类动作；后续若需要维护外部业务状态，应先明确幂等与提交结果不确定时的处理契约。

---

## 验证与变更入口

[离线模式测试](../../../../tests/test_agent_modes.py)验证模式与工具集合隔离、资料来源与状态、参数验收、异步执行结果、纠错清理、工作区预演隔离、取消与故障、提交失败不暴露结果以及提示词/工具计数一致性。[独立 MySQL 测试](../../../../tests/test_agent_modes_mysql.py)验证真实落盘、助理压缩后资料保留、探索分支恢复、用途不匹配、跨用户资料及并发工具身份隔离。测试模型和业务服务均为替身，数据库仅使用一次性临时容器，不访问实际配置或共享业务服务。

本次新增文件为上述模式包（共 12 个文件）、共用 `app/agent/context.py`、两份测试和本说明；修改 Core 的提示词组合、消息渲染、工具注册与运行循环，以及 QuestionAgentService、包导入测试和文档导航。未增加实际的 profile/memory/calendar 工具文件。

2026-10-21 验证：完整 367 项通过，无跳过，包含 69 项独立临时 MySQL/MyVector 验证；本次新增 8 项离线与 8 项模式实库测试。`pip check` 无依赖冲突，本次维护的 6 份文档共 59 个相对文件链接有效。临时容器与临时卷在验证后清理，未修改真实 `.env`、业务数据库或其他项目，未执行真实业务服务或模型联调。


---

## 文件检索依赖

助理和探索`build_service(..., file_retrieval=...)`均支持[注册中心文件查询](file-retrieval.md)。当前任务按可信userid创建独立结果池，读取权威SQL权限；主工具只装配到本轮Core，不修改共享注册表。查询、分页不会打开或下载原件。


---

## 注册中心原件依赖

两种模式的`build_service(..., registry_files=...)`注入共享下载客户端，任务执行时通过`access_token=SecretStr(...)`传入已认证用户的Bearer。查看器按本轮绑定，PDF模型与上传附件共用LRU，任务结束清理凭据；详见[注册中心文件图像查看](registry-file-reading.md)。


---

## 探索中途输出与沟通提醒

`exploration/tool/emit_progress.py` 独立定义 `emit_progress` 与必填 `content` 参数，声明 `output_kind=intermediate`；成功提交与交付后继续运行。相应主动沟通规则位于 `exploration/prompt/interaction.py`；开场和周期提示位于 `exploration/prompt/guidance.py`，仍复用 Core 的统一 `system-guidence` 格式构造器。

Core 和助理模式均不注册 `emit_progress`，静态提示词不包含主动中途反馈要求。助理将同名 `finish_task` 扩展为必填 content 和 send_to_user，并在消费宿主中装配独立的 send_assistant_message；公共 Core 与探索的最终工具参数不变。助理对话采用单任务、最终完整回复方式；本次不新增助理 HTTP 接口。探索继续保留中途及最终回复的 SSE 分片、展示 ID、取消及历史恢复协议。

新任务首次主模型请求会追加参考项目同款 `action_guidance`：建议优先调用 `emit_progress`，简短说明对需求的理解和下一步；如果已经能够直接回答，可直接调用 `finish_task`。这是可选沟通建议，不强制开场消息，不伪造固定SSE正文；实际输出继续走[流式交付](exploration-session.md#sse-与恢复)。

开场建议只出现在第一次生成请求，参数纠错或后续工具轮不重复。首次请求若先做上下文压缩，重建后仍保留一条开场建议并重新计数；同一请求不再叠加定时提醒。它不进入权威交互、纠错轨迹、摘要材料、工作区或历史消息。历史祖先不属于当前任务执行轨迹，不抑制新任务的开场建议；当前Core拒绝重放已有有效交互的任务。关闭周期提醒不会关闭首次开场建议，无新增配置项。

[ProgressReminder](../../../../app/agent/exploration/progress_reminder.py)按每次主循环运行独立维护单调时钟，与`contract-service-be`的定时沟通建议语义一致。共享Core或并发用户不会共享计时状态；计时从本次主循环开始，不使用历史节点时间或上一次任务的反馈时间。

配置由[Agent配置加载器](../../../../app/config/agent.py)读取，仅探索工厂装配该策略；配置继续由共享运行选项承载，给探索工厂显式传入`CoreSettings(progress_reminder_interval_seconds=60)` 即可覆盖。直接构造 Core 或助理工厂不会因设置该值而自动启用提醒。环境变量优先于文件，非法配置本地失败且不回显原值。

```dotenv
# 默认60秒；允许非负有限小数，0关闭。与单次模型请求超时独立；整轮任务不设置总时限。
AGENT_PROGRESS_REMINDER_INTERVAL_SECONDS=60
```

- 到达间隔后，在下一次主模型请求的最后追加一条`action_guidance`，建议调用`emit_progress`说明已确认发现、待确认事项和下一步；没有新发现时说明实际阻碍，不编造进展，不展示内部思考。若可直接交付，仍允许`finish_task`结束任务。
- 提醒是软建议，不能保证每60秒必定产生用户消息；不会中断正在执行的模型请求、工具或压缩，不启动额外定时线程，也不伪造SSE中途输出。长操作完成后，下一轮模型请求才会看到提醒。
- 未成功反馈时，每次后续模型请求继续提醒，但每份请求最多一条。参数错误、普通工具成功、工具自动SSE状态、上下文压缩均不会重置计时。
- 只有`emit_progress`完成权威提交且宿主`publish_stream`或`publish`交付成功后才重计；无交付回调的内部调用以权威提交为准。提交失败、交付失败或取消沿用原有停止语义，不重计、不重复发送。最终答复可直接结束，不要求额外插入中途消息。
- 提醒仅加入请求副本，纳入实际token计数和容量检查；压缩后重建请求会重新追加、重新计数。它不进入`accepted`、临时纠错轨迹、压缩源、节点公开历史或工作区，不在任务恢复时作为记忆回放。模型原生响应仍按既有私有审计规则保留。

示例：连续阅读资料超过60秒，下一请求收到沟通建议；模型继续查找而未反馈，则后续请求仍提示。成功向用户输出“已找到两份相关资料，数值尚有差异，接下来核对原件”后重新计时。文案必须符合实际结果，并非固定播报内容。

[提醒隔离测试](../../../../tests/test_progress_reminder.py)使用模拟时钟、模型和内存journal验证边界时间、持续提醒、成功重计、失败/取消、压缩与计数、任务隔离及最终输出；[配置测试](../../../../tests/test_agent_config.py)覆盖默认值、关闭、小数、环境覆盖、配置载入及非法值。无需外部服务或真实运行数据。


探索工厂为 `QuestionAgentService` 注入 `output_observer_factory`，每次正式执行按本轮工具注册表创建观察器。助理未装配流式适配器；若内部调用误传 `publish_stream`，在身份处理、附件与节点写入之前明确拒绝，不悄悄丢弃回调。`with_registry` 保留请求提示工厂，实际计时器每次 `run` 新建，用户间不共享状态。模式隔离与流式归属见[专项测试](../../../../tests/test_exploration_feedback.py)。


本次迁移保留 `AGENT_PROGRESS_REMINDER_INTERVAL_SECONDS` 名称、默认 60 秒与 0 关闭周期提醒的语义，`.env.example` 注明仅作用于探索模式；实际 `.env` 未修改。公共历史恢复仍可读取既有 `emit_progress` 轨迹，不改变已落盘消息或历史终态规则。


---

## 本次迁移文件与验证

以下路径相对于项目根目录；仅列本次探索反馈迁移的修改，不包含工作区已有的其他变更。

| 新增、迁移或修改路径 | 位置与目的 |
| --- | --- |
| `app/agent/exploration/tool/emit_progress.py`、`app/agent/exploration/tool/__init__.py` | 独立定义、组合 `emit_progress`，声明中途输出与参数契约。 |
| `app/agent/exploration/prompt/interaction.py`、`app/agent/exploration/prompt/guidance.py`、`app/agent/exploration/prompt/__init__.py` | 探索静态中途沟通规则、开场/长执行建议及提示词组合。 |
| `app/agent/exploration/progress_reminder.py` | 从 `app/agent/core/progress_reminder.py` 迁入；每次运行独立计时，原路径删除。 |
| `app/agent/exploration/output_stream.py` | 从 `app/agent/core/output_stream.py` 迁入；按已注册输出类别产生 SSE 展示事件，原路径删除。 |
| `app/agent/exploration/factory.py` | 注入探索沟通策略和流式观察器工厂。 |
| `app/agent/core/guidance.py`、`app/agent/core/output.py` | 新增通用请求提示、公开输出观察协议，不包含探索文案或 SSE 实现。 |
| `app/agent/core/runtime.py` | 创建独立提示策略、按注册输出类别提交、调用可选观察器；默认无中途工具和提醒。 |
| `app/agent/core/tool/base/finish_task.py`、`app/agent/core/tool/registry.py` | 定义最终输出抽象契约，校验模式实现及 `output_kind` 内部注册声明。 |
| `app/agent/core/prompt/guidance.py`、`app/agent/core/prompt/interaction.py`、`app/agent/core/prompt/__init__.py` | 移除探索文案，保留公共反馈模板及模式规则注入点。 |
| `app/agent/core/model.py`、`app/config/agent.py`、`.env.example` | 标明提醒配置仅探索使用，名称与默认值保持不变。 |
| `app/service/question_agent.py` | 模式注入流式适配器，按任务创建；未装配时在写入之前拒绝流式调用。 |
| `description/architecture/workflow/question-answering/agent-modes.md`、`agent-core.md`、`package-structure.md`、`exploration-session.md`、`context-rendering.md`、`readme.md`（均位于同一目录） | 更新模式分工、调用契约、工具数量、目录、历史恢复及配置说明。 |
| `tests/test_exploration_feedback.py` | 新增 7 项模式隔离、真实服务装配、注册 Schema、SSE 与依赖方向验证。 |
| `tests/test_progress_reminder.py`、`tests/test_output_stream.py` | 迁移测试入口，保留提醒时间边界、并发隔离、真实分片消费、取消和交付失败覆盖。 |
| `tests/test_agent_core.py`、`tests/test_agent_core_packages.py`、`tests/test_agent_modes.py`、`tests/test_agent_config.py`、`tests/test_tool_tag.py` | 更新七个公共工具与探索额外工具的装配/配置/Schema 断言。 |
| `tests/test_fifo_alignment.py`、`tests/test_core_context_rendering.py`、`tests/test_image_tool.py`、`tests/test_tool_content_contract.py` | 需要中途输出的测试显式装配探索工具；与中途输出无关的折叠测试改用公共工具推进。 |
| `tests/fixtures/agent_core_contract.json`、`tests/fixtures/core-rendering/messages.json`、`tests/fixtures/core-rendering/messages.md` | 更新公共工具与提示词快照；公共 Core 请求样例不再带探索开场提醒。 |

2026-10-24 隔离回归：`python -m unittest discover -s tests` 共 1085 项，923 项通过，162 项因未配置独立 MySQL/MyVector 环境跳过，无失败。模式专项 7 项通过；包含此前提醒与 SSE 测试的完整回归仍通过。相关六份文档的 124 个相对链接/锚点与 `git diff --check` 通过。未调用真实模型、共享数据库或中间件，未修改实际 `.env` 或其他项目，未新增助理 HTTP 接口。
