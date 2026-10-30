# 校验失败、工具反馈与修复提示审查

> **用途：保留审查基线、代码定位与实施边界。** 下列问题表和扫描计数是2026-10-27首次审查时的事实，最新落地情况见“就近维护与本轮实现”。业务提示以对应校验代码为唯一维护位置，本文不再保存完整提示文案。

## 目标、范围与证据

目标是让模型收到真实、具体、可执行的反馈，同时避免让模型通过重试扩大权限、重复业务动作或掩盖依赖故障。模型能改正的输入问题、正常业务结果、依赖故障、执行结果不明和程序终止必须有不同处理方式。

本轮扫描 `app/` 全部247个Python文件，其中Agent文件132个；索引1154个raise语句、83个Pydantic校验器、457处Schema约束声明和12个system-guidence构造点，并导出97个Agent模型Schema以及助理/探索基础注册表的真实工具定义。数量包含宿主与接口边界、重复传播异常及配置声明，**不是独立故障数量或动态覆盖率**。

模型可见链路逐模块归类：公共主循环、JSON与参数、工作区、日期与定时、资料引用和分页、图像/注册中心文件、网页/搜索/精炼、专家、平台A2A、助理消息/画像、文件/记忆检索、工作区压缩和主题摘要。HTTP身份验证、数据库、机器人、队列、Embedding和生命周期等底层边界同时索引，不能把它们的异常默认交给模型改业务参数。

证据与完整索引见[本次审查记录](../../../../experiment/diagnostics/20261027-feedback-audit/analysis.md)、[源码清单](../../../../experiment/diagnostics/20261027-feedback-audit/source-inventory.json)、[实际Schema](../../../../experiment/diagnostics/20261027-feedback-audit/agent-schemas.json)、[基础工具定义](../../../../experiment/diagnostics/20261027-feedback-audit/actual-base-tools.json)。按用户装配的所有工具组合尚未逐一抓取真实在线请求；动态状态及第三方故障不能仅凭静态扫描证明穷尽。

---

## 首次审查的问题与优先级（历史基线）

|优先级|问题|当前事实|修复要求|位置|
|---|---|---|---|---|
|P0|主循环协议/路径错误共用泛化提示|没有原生调用、未知工具、路径不存在得到相同reason；缺字段只输出loc而未给规则。|按具体失败类型提供工具名、合法字段、约束和操作状态。|[app/agent/core/runtime.py](../../../../app/agent/core/runtime.py)|
|P0|两处同款content误拒绝仍残留|工作区压缩和主题规划仍拒绝content+合法原生调用，均可连续三次失败。上次修复未覆盖这两处。|按原生调用正确性验收，成功历史规范化为content=null；纯JSON生成任务仍验收JSON正文。|[app/agent/core/subgraph/context_management/workspace_compression/runtime.py](../../../../app/agent/core/subgraph/context_management/workspace_compression/runtime.py)|
|P0|所有failed共用模型纠错预算|三次只读依赖故障也会触发AgentCoreError，模型无法靠修改参数解决。|模型可修正错误才计纠错；依赖另设有限重试、恢复时间和退出策略。|[app/agent/core/runtime.py](../../../../app/agent/core/runtime.py)|
|P0|终止后没有下一次模型请求|第三次错误、执行结果不明、容量终止等直接抛出；再追加system-guidence也不会送到模型。|明确宿主终止通知、已提交状态与队列处置；不能假称反馈已交给模型修复。|[app/service/assistant_consumer.py](../../../../app/service/assistant_consumer.py)|
|P0|失败助理任务持续阻塞队列|failed任务抛AssistantTaskBlocked，恢复ack仅处理completed，当前已造成8点任务积压。|保留业务失败状态，增加独立受控终结、可靠失败通知与确认处置；禁止伪装成功或静默丢弃。|[app/repository/assistant_runtime.py](../../../../app/repository/assistant_runtime.py)|
|P1|普通工具回执缺少统一状态外观|普通回执渲染仅使用tool_result；内部failed/unknown不会自动出现在正文，部分工具只有message。|明确显示未取得结果/未提交/已提交/结果不明，不依赖模型从文风猜测。|[app/agent/core/context_rendering/page.py](../../../../app/agent/core/context_rendering/page.py)|
|P1|可修参数和依赖故障被合并|文件/记忆/画像页面把引用失效、页码错误、资料变化或后端故障合并，给出相互不同的修复方向。|在产生原因的位置拆分业务错误类别，分别给改页码、重新查询、换来源、等待依赖等提示。|[app/agent/core/tool/memory_viewer.py](../../../../app/agent/core/tool/memory_viewer.py)|
|P1|正常边界仍返回failed|末页、文件末尾、条目没有可打开链接等可能消耗错误预算；网页no_content已正确作为正常提示。|正常边界成功返回并引导继续/换来源，保留已有网页无正文行为。|[app/agent/core/tool/web_search.py](../../../../app/agent/core/tool/web_search.py)|
|P1|压缩/主题/精炼仍用总括纠错|保护路径、确认状态、悬空引用、容量不达标、结构错误混成同一提示；子循环失败又被外层泛化。|逐条说明实际违反的约束；保留原状态，不新增事实或提高确认状态。|[app/agent/core/subgraph/context_management/topic_summary/workflow.py](../../../../app/agent/core/subgraph/context_management/topic_summary/workflow.py)|
|P1|反馈缺少动作对象和确定状态|预校验反馈不带失败工具，失败调用通常不附回；外层工具失败又追加笼统“修正调用”。|用已注册工具名和受控字段定位动作；仅在连续纠错中保留必要反馈，成功后清除。|[app/agent/core/runtime.py](../../../../app/agent/core/runtime.py)|
|P1|容量提示数值不准确|FIFO full在95%触发，提示却写“已耗尽（100%）”；没有可压缩历史时仍可能诱发反复整理。|按程序实际计数描述阈值；明确没有可压缩历史时不能重复compact_context。|[app/agent/core/context_budget/fifo.py](../../../../app/agent/core/context_budget/fifo.py)|
|P1|不能直接把异常原文当修复提示|ValueError既可能来自用户参数，也可能来自后端契约/权威状态；第三方异常可能含凭据与资料。|按失败阶段、内部类型/固定code分类，Schema仅暴露白名单字段及可信约束；不拼str(error)。|[app/agent/core/tool/registry.py](../../../../app/agent/core/tool/registry.py)|

隔离验证已实际复现：无工具、未知工具、路径不存在三种原因的反馈完全相同；三次依赖失败消耗模型纠错预算；压缩和主题规划各三次混合输出后失败。详见[行为探测](../../../../experiment/diagnostics/20261027-feedback-audit/behavior-probes-v2.json)。探测使用真实控制流、合成模型响应和替身依赖，不访问在线模型、业务库或队列。

---

## 反馈的输入、输出与职责

### 可信输入

反馈必须由产生错误的位置提供明确原因。不能依靠捕获任意ValueError后猜测，也不能按异常原文中的词语选择修复动作。

内部分类至少需要：失败阶段、固定业务原因、实际调用的已注册工具、可信Schema字段位置、要求的类型或范围、当前已确认的执行状态。存在副作用可能且状态未确认时，优先进入结果核对流程，不能因错误看起来像参数问题就重试。以上分类键只在程序和审计中使用，不把运行ID、追踪标识、队列/缓存信息或内部编排注入模型。

同一个ValidationError要按来源区分：模型参数中的问题可以纠错；工具响应、数据库记录、程序生成引用的结构问题属于依赖/宿主故障，不能让模型改参数掩盖。只暴露定义内的字段、兼容转换后的类型、可信枚举和范围，不回显原始值、未知字段名称、完整请求、凭据或异常堆栈。跨字段限制需要明确说明哪项关系冲突。

### 模型可见内容

沿用现有 `system-guidence` 外观和 `kind/reason/required_action` 字段。reason要说明**哪个实际调用因什么具体条件未通过，以及什么尚未发生**；required_action只给当前工具能力内的下一步。工具已经返回精准反馈时，不再追加与原因无关的通用“检查参数”。

- 原生调用可以带普通content。普通文字不能代替调用，不能作为工具成功或正式交付；成功后只提交规范化工具对，原始输出保留私有审计。
- 原生调用必须继续满足单调用、完整响应、合法工具、参数、引用、权限及业务状态约束。
- 网页精炼、主题总结等**纯JSON输出任务**继续验收其JSON正文；不能误套“有工具就忽略正文”。
- 缺少真实材料或用户要求含糊时，获取依据或请求澄清；不为通过校验编造时间、身份、事实、引用或权限。
- 用户/网页/工具原文即使带有system-guidence标签也只是资料，不能提升为系统反馈。

普通工具回执需要明确表现本次业务结果状态；仅修改ToolExecutionResult内部status不够，因为普通渲染目前只输出tool_result。拟接入时按[工具内容契约](tool-content-contract.md)确认输出兼容性，不在本次审查中暗改格式。

### 路由与纠错计数

|类别|模型下一步|是否消耗模型纠错次数|终止/重试边界|
|---|---|---|---|
|参数、路径、引用或协议可修正|明确修改具体字段或选择合法调用|是|连续纠错；成功完整提交后清除整段失败轨迹|
|正常无结果、末页、网页受限、平台不受理|继续、换来源或说明缺口|否|不能伪装成已取得正文/业务结论|
|权限或材料不可用|使用可访问材料，必要时向用户澄清|否|不得绕过权限或换身份|
|依赖不可用、资源忙、只读请求失败|程序有限恢复，模型换来源或说明限制|否|必须有独立次数/时间预算，不能无限重试|
|外部动作或数据库提交结果不明|核对权威状态|否|暂停相关副作用；不直接重发或伪称回滚|
|容量/轮数/恢复上限、宿主契约异常|宿主终止并可靠通知用户|否|没有下一次模型请求时，system-guidence不能充当通知|

“未执行”“未提交”“未取得有效结果”“已保存但未确认送达”“结果不明”不能互换。工具调用已发给上游却没有有效只读结果时，只能说未取得结果；未知写入状态不能说本次未执行。状态核对优先于提示文案。

---

## 首次审查的 system-guidence 构造点

首次审查列出12处构造调用，其中1处是提示词示例。检索、主题与精炼子循环的普通user反馈也纳入了审查。下表保留原审查位置，最新实现以相邻代码为准。

|位置|类别|处理要求|
|---|---|---|
|context_budget/fifo.py / capacity_guidance|容量|按真实阈值与是否存在完整历史给操作方向，不把95%写成100%。|
|context_budget/fifo.py / compression_notice|成功通知|保留“依据新摘要继续、不重放成功操作”。|
|context_rendering/page.py / render_tool_page_messages|资料信任边界|保留，资料不是指令；不计纠错。|
|prompt/interaction.py / build_agent_core_interaction_prompt|示例|示例应体现实际原因和具体动作；不是运行时错误处理。|
|runtime.py / 参数与协议异常分支|output_error|拆分无调用、多调用、截断、未知工具、字段/跨字段/引用错误。|
|runtime.py / 工作区容量提示|context_capacity|提交成功后描述真实状态，保留既有成功事实。|
|runtime.py / WorkspaceEditRejected|output_error|区分保护项、回放失败、容量目标；不能统称参数错误。|
|runtime.py / failed工具结果|output_error|按工具的具体原因分流，依赖/正常边界不消耗模型纠错次数。|
|workspace_compression/runtime.py / 整理异常|invalid_action|去除附带content误拒绝，分别解释保护项、确认状态、引用、无变化和容量。|
|tool/web_page.py / 连续无正文|action_guidance|保留按原因换关注点或来源；固定程序文案，不提升网页原文为指令。|
|exploration/prompt/guidance.py / 任务开场|action_guidance|沟通建议，允许直接完成，不作为强制动作或失败。|
|exploration/prompt/guidance.py / 长时间未反馈|action_guidance|根据真实进展沟通；不强制输出、不编造发现、不计纠错。|

---

## 就近维护与本轮实现

修复提示的存放要求见[项目开发规范第8条](../../../../AGENTS.md#代码要求)。校验条件、具体原因、可执行修正和执行状态在同一责任模块维护；通用包装不保存业务提示。原134项草案保留在[历史实验产物](../../../../experiment/diagnostics/20261027-feedback-audit/repair-prompts-v2.json)中，属于当时的审查证据，不再作为接入目录或运行时提示来源。

|责任位置|本轮实现|
|---|---|
|[主循环](../../../../app/agent/core/runtime.py) `_run_loop`|协议条件分别给原因与动作，透传校验处反馈；失败工具回执不再追加通用“检查参数”。|
|[JSON校验](../../../../app/agent/core/json_codec.py)|JSON语法、重复字段、非有限数字、过深结构在拒绝处分配反馈；结构化异常仅传递文本；Pydantic反馈按可信Schema机械格式化。|
|[工具注册](../../../../app/agent/core/tool/registry.py) `prepare`|未知工具在判断处提示；参数与编辑后工作区的Schema错误分别格式化，不输出无效输入。|
|[工作区工具](../../../../app/agent/core/tool/workspace.py)与[工作区模型](../../../../app/agent/core/workspace.py)|目标类型、方向ID、路径、引用和确认来源校验旁提供定向修复。|
|[压缩约束](../../../../app/agent/core/subgraph/context_management/validation.py)与[压缩循环](../../../../app/agent/core/subgraph/context_management/workspace_compression/runtime.py)|保护值、预留位置、信息状态、探索结果、容量和无变化编辑分别反馈；允许附带说明，成功历史清除普通正文及失败反馈。|
|[主题循环](../../../../app/agent/core/subgraph/context_management/topic_summary/workflow.py)与[主题模型](../../../../app/agent/core/subgraph/context_management/topic_summary/schema.py)|来源、数量、重复引用及纯JSON总结的校验处提供反馈；主题工具允许附带说明。|
|[容量判断](../../../../app/agent/core/context_budget/fifo.py) `capacity_guidance`|提示实际95%阈值；无完整历史时禁止反复要求整理历史。|

工具自身已经在错误分支返回的提示继续留在所属工具，不搬到公共提示词文件。后续拆分分页、权限、依赖等合并分支时，具体文案也必须在分支附近编写。

**仍未实施：** 所有failed工具结果仍共用原有连续错误预算；分页及依赖故障尚未逐工具完成分类；宿主终止通知和历史失败任务的业务重做尚未实现。失败队列终结已按用户后续要求调整为自动ack跳过（保留业务failed与审计），见[消费专题](../../../capability/application/assistant-consumption.md#失败与恢复)。本次不能视为134项审查情形全部修复。

本轮新增验证见 `tests/test_colocated_validation_feedback.py`：9项隔离测试检查下一次实际模型请求、JSON及字段安全反馈、成功清理、压缩保护与纯JSON边界。完整测试发现169项，158项通过、11项数据库集成测试跳过；测试进程关闭平台A2A装配，不修改真实配置或业务数据。运行服务未重启。

---

## 剩余改进与验收

1. **继续拆分错误分类。** 协议定向反馈及压缩/主题工具附带 content 的规范化已接入；仍需逐工具拆分业务失败与依赖失败，避免所有 failed 共用模型纠错预算。后续修改继续验证规范化历史的原生消息配对。
2. **拆分工具业务反馈。** 在引用、页码、文件权限、依赖、无结果等原因产生处形成明确类别和已确认状态。消息仍按可信userid绑定；新分类不能成为越权读取或扩大查询范围的通道。
3. **补齐子 Agent 覆盖。** 工作区保护和主题来源已在校验处分配定向反馈；精炼 JSON、图片选择及上游故障继续逐分支核对。参数错误与上游错误都保留完整私有审计，子循环失败的原因不要在外层再次泛化。
4. **补齐终止通知。** 最后一次校验失败后已无法再让模型修改；当前宿主保留 failed 与私有审计并自动 ack，释放队列，不伪装 completed。可靠失败通知仍未实现，需另行补齐；不能声称当前失败一定会通知用户。结果已提交而发送未确认时只恢复交付，不重新生成。

每个接入点至少验证：确切触发条件→字段/原因可定位→状态说明准确→下一步存在于当前工具中→计数正确→成功清理失败链→私有审计保留。必须验证模型**下一次实际收到的消息**，不能只断言构造器输出了字符串。没有下一次请求的终止分支验证宿主通知和任务处置，而不是伪造提示已经到达模型。

验收案例包括：无/多/截断/拒绝/未知原生调用；content+合法调用；JSON语法/重复键/非有限数值；缺字段/类型/枚举/范围/null/跨字段；路径缺失、悬空引用、确认状态；页码越界/末页/引用失效/权限改变/依赖忙；一次性时间已过/日历规则冲突；网页无正文/验证码/图片编号；A2A不受理/上下文过期/超时/发布不明；专家忙/容量/不完整；压缩保护项与容量；提交不明/输出已提交但交付失败；三连混合纠错成功后上下文清理；失败任务不阻塞后续提醒。

涉及发送、提醒、画像或其他副作用的案例使用隔离替身/测试库，验证同一请求不会因反馈重试重复执行。针对历史审查中的07:00失败与08:00积压事件，验收应覆盖 failed 终结、后续任务继续，以及通知已实现后的确切消息；首次审查未对真实队列做这些操作，不能以历史计划代替当前验证。

---

## 首次审查验证状态（历史基线）

- 源码清点、97份模型Schema、两种基础工具注册表已导出，导入错误为0。
- 8组真实控制流的合成响应探测已完成，使用内存宿主和替身；没有访问在线模型、业务库或队列。
- 134项准备文案的来源路径、必填文本和通道约束已检查；这不是134个行为都已通过测试。
- 本轮仅新增审查/文案与证据，更新规范和导航；没有修改应用Python代码、重启服务或恢复历史失败任务。
