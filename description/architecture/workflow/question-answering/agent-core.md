# 核心 Agent 循环

## 输入与职责

实现为 [AgentCore](../../../../app/agent/core/runtime.py)，请求、结果与运行选项见[调用模型](../../../../app/agent/core/model.py)。输入由用户问题及三类内容组成：

| 输入 | 契约 |
| --- | --- |
| 上下文注入 | ContextInjection：摘要、公开历史、所选工作区及本次补充资料；不接受任意消息角色执行。 |
| 思考强度 | none / low / medium / xhigh；none 关闭原生思考，其余映射既有生成配置。 |
| 用户平台等级 | platform_permissions 映射，由可信 AuthenticatedUser 提供；缺少键不授予权限，零等级也不代表管理权。 |

`AgentInput.for_user` 与 `QuestionAgentService.generate(user=...)` 用服务端身份装配等级；HTTP 层不采纳前端传来的等级覆盖身份。平台等级作为任务边界信息注入，不承担管理员鉴权。

主循环按参考项目拆分摘要、历史任务、工作区和当前输入，保留真实 assistant/tool 角色及已接受的原生调用，不将历史压成一条 user 引用文本。工作区使用中文区域、真实路径与 YAML，正文使用安全围栏；完整契约及实际样例见[上下文渲染与消息组织](context-rendering.md)。动态FIFO计数与正式生成共用当前输入渲染，固定预算只包含系统、工具和空消息外壳。摘要后保留相同问题、资料、权限和本轮活动轨迹。

系统角色、交互和工作区规则按[参考项目的组件方式](package-structure.md)分别维护，由 prompt 包统一装配。用户文本中的标题、角色字样和指令仍是普通资料，不反解析为额外消息。模型消息不含树/节点 ID、运行 ID、revision、提示词版本或编排信息；原生函数协议的 call_id 和工作区业务条目键仅用于合法工具交互。上下文整理子 Agent 的结构化输入和工具 JSON 协议保持独立。

Core 可接收宿主组装的 `system_prompt` 或 `prompt_builder`，以及 ToolRegistry；角色可使用通用默认值，工具集合必须由宿主显式装配六个直接实现及一个模式最终输出。个人助理及探索模式通过[独立工厂](agent-modes.md)装配，不在循环中判断模式；所有有效提示词与工具均计入实际预算。可选的 [tool-tag](../../../capability/infrastructure/tool-tag.md) 在创建宿主时加载一次，主循环、压缩与纠错共用同一快照；关闭不影响原生工具及业务约束。

---

## 运行循环

每轮依次计数与必要整理、调用模型、记录完整私有生成、检查单工具协议、预演参数和业务规则、持久化提交，最后发布已接受的公开输出。普通文本、多工具、截断、拒绝、重复调用 ID、未知工具、重复 JSON 属性及非法数值均不能执行动作。

错误反馈由 prompt/guidance.py 统一构造，只在当前连续纠错期间存在，包含最小字段位置或修正方向，不回显无效参数。一个动作完整验收并提交成功后，整段失败反馈清除，只有正确工具对进入有效轨迹。原生推理不作为公开消息，也不自动再次注入。

[工具注册表](../../../../app/agent/core/tool/registry.py)统一绑定名称、说明、参数模型及无副作用预演，注册时递归校验参数字段说明，拒绝重名。date_calculator、workspace、interaction、context 四个能力模块各自提供注册项，路径消歧解析器显式绑定，注册表不根据名称前缀分派。定义顺序稳定，运行时只使用已注册工具。对象/数组字段允许依据实际 Schema 解码 JSON 字符串；字符串字段保留原样，语法不修复，解码后仍执行完整 Pydantic 与业务校验。

公开输出工具通过 `output_kind=intermediate/final` 声明提交语义，注册时必须有必填字符串 `content`；Core 与观察器按注册声明处理，不硬编码探索工具名。

专用读取工具可配置 executor，参数和候选状态验收后执行，统一返回 `ToolExecutionResult`。注册声明 `ordinary/foldable` 内容能力，宿主执行前验收支持范围，执行后校验实际类型；`failed` 进入受限临时纠错，`unknown`、异常或提交故障停止且不自动重放。普通结果与图片、文本、表格页面共用[工具返回与折叠契约](tool-content-contract.md)。图像游标通过内部 `ToolExecution.after_commit` 在权威提交后推进。模式入口可按可信用户装配本轮独立工具集合，执行器仍负责业务授权。

---

## 任务开场与长任务反馈提醒

该能力属于[探索模式反馈](agent-modes.md#探索中途输出与沟通提醒)，由探索工厂注入 `guidance_factory`。Core 默认无沟通提醒，也不注册 `emit_progress`；助理模式只交付最终回复。

Core 每次运行创建独立 `RequestGuidance`，在正式主模型请求前调用 `before_request(round_number)`，返回的临时消息计入请求预算，但不写入历史、摘要和工作区。输出提交并成功交付后才调用 `after_output(kind)`；失败或取消不通知成功。Core 不决定开场文案、时间间隔或具体输出工具。

---

## 运行时缓存与持久化边界

以下LRU均为Python内存结构，不通过SQL实现，也不因打开或分页而保存第二份持久化缓存。文件检索此次新增摘要向量内存LRU后，共有11处LRU实现（含两处按用户/树组织的外层资源池）：

| 实现 | 内容与范围 | 释放边界 |
| --- | --- | --- |
| `FileModelCache` | 用户/树/文件版本键，上传附件与registry原件共用PDF模型池 | 淘汰或宿主关闭；等待在用租约归还。 |
| `ExternalExpertSessionPool` | 同一用户树的专家会话历史 | 淘汰、释放树资源或宿主关闭。 |
| `ExpertSessions` | 按用户/树组织专家资源池 | 外层淘汰、显式释放或关闭。 |
| `WebSearchPool` | 同一用户树的搜索结果集 | 淘汰或资源关闭。 |
| `WebPageViewer` | 网页正文与页面资源 | 淘汰或关闭；临时图片随任务结束释放。 |
| `WebSessions` | 按用户/树组织网页资源池 | 外层淘汰、显式释放或关闭。 |
| `MemoryQueryPool` | 本次任务的祖先记忆召回排名 | 淘汰或任务结束。 |
| `CandidatePool` | 单次文件检索子智能体候选集合 | 淘汰或子检索结束。 |
| `FileResultPool` | 本次问答的最终文件结果集、分页位置 | 淘汰或任务结束。 |
| `ExplorationTreeService._loaded` | 按用户/树加载的问答树快照 | 淘汰、树删除或服务关闭。 |
| `MemoryFileSearchIndex._vectors` | 按内容/模型契约寻址的摘要向量；不存权限 | 有界淘汰或服务关闭，每次使用前重新读取授权范围。 |

文件检索的名称/摘要词频只活到单次查询结束，参见[文件查询内存索引](file-retrieval.md#检索索引与配置)。上述池可以从持久化权威数据加载，也可以留下必要的任务历史/私有模型审计；这不表示LRU条目本身被保存进数据库。

问答树、节点消息、完整原生生成、工作区快照、`question_node_memories`和`question_memory_index`仍按[记忆存储契约](../../data/question-memory.md)持久化。后者是问答记忆全文/向量索引，不是`MemoryQueryPool`。上传附件原件仍保存在既定用户文件目录；文件图像LRU是对原件的临时解析。删除文件检索数据库缓存不改变这些既定职责。

---

## 必要工具

| 工具 | 动作 |
| --- | --- |
| get_current_time | 获取执行时的北京时间、时区及星期，普通结果；SSE 为 thinking。 |
| calculate_date | 有依据的开始时间加减年/月/日/时/分/秒，处理月末及闰年，普通结果。 |
| workspace_add | 新增用户补充、事实或剩余规划；提交探索结果时原子收束已有方向。 |
| workspace_replace | 更新已有字段或完整条目，不覆盖无关信息。 |
| workspace_delete | 删除完整条目，拒绝删除仍被引用的信息和必填容器。 |
| finish_task（模式必须实现） | Core 提供抽象契约，模式定义最终结果与交付选择；提交后结束本轮。 |
| list_sub_management_platforms（装配平台目录时） | 查询子管理平台的五项信息，以可折叠正文展示，离线平台保留。 |
| search_web（节点服务装配） | 搜索公开网页，返回结果集 ID 和条数。 |
| view_web_search_results（节点服务装配） | 分页查看候选；折叠正文与提交后游标。 |
| open_web_page（节点服务装配） | 按 focus 抓取并精炼正文，或读取已有正文分页。 |
| ask_external_expert（节点服务装配） | 首次专业求助，可选固定专家职责，返回新专家会话及完整答复。 |
| continue_external_expert（节点服务装配） | 在同一用户树的专家会话中追问，成功历史由程序维护。 |
| compact_context | 整理完整历史任务前缀并合并既有摘要，保留尾部与本轮轨迹，验收后继续。 |
| view_image_file（装配文件服务时） | 仅阅读用户图像附件；指定页码或省略以继续下一页，使用 LRU 文件池和临时原生图片。 |

[主题整理](topic-summary.md)分为 extract_topic / finish_topic_planning 规划、无工具的 JSON Schema 总结及独立 工作区增删改及 finish_compression 精简循环，不增加业务工具。工具不接收 userid、平台凭据、树/节点标识或 expected_revision；这些身份和版本在程序绑定的 journal 中维护。

---

## 当前时间工具

[current_time.py](../../../../app/agent/core/tool/current_time.py) 提供 `get_current_time`，默认注册到公共 Core，探索与助理模式均可用。调用参数为 `{}`，拒绝额外字段；每次执行读取服务端系统时钟，显式采用北京时间 `UTC+08:00`，不依赖宿主机或容器的本地时区。

返回普通工具结果（`ordinary`，不可折叠）：

```json
{"current_time":"2026-10-24T23:59:59.123+08:00","timezone":"UTC+08:00","weekday":"星期六"}
```

SSE 动作类型显式注册为 `thinking`，动作文本为“正在获取当前时间”；执行结束沿用统一机制恢复“正在思考”。工具结果通过正常提交进入模型轨迹，不自动发布中途答复或修改工作区。

用户询问此刻时间，或长任务需要最新时间时调用。回执只代表调用当时，后续需要实时值应重新调用；它不改写任务创建时间，也不改变历史请求中相对日期的基准。日期加减仍交给 [calculate_date](#日期计算工具)。无需新增配置、依赖、网络请求或数据库表；系统时钟准确性由运行环境保障，读取异常不返回伪造时间。

[隔离测试](../../../../tests/test_current_time.py)覆盖显式时区、跨日与星期、重复调用刷新、空参数约束、实际模型 HTTP 工具定义、Core 回执与 thinking 状态，以及时钟异常。

---

## 日期计算工具

[date_calculator.py](../../../../app/agent/core/tool/date_calculator.py) 对齐 contract-service-be 的无状态日历运算，默认注册到公共 Core，助理与探索模式均可用。主循环统一执行、校验和归档，不访问网络或数据库；不改工作区、不发公开答复。返回 `ordinary`，不折叠，SSE 动作采用本项目默认 `thinking/正在思考`。

| 参数 | 含义 |
| --- | --- |
| `start_time` | 必填；有效 `YYYY-MM-DD` 或包含时分秒的 ISO 日期时间，可带小数秒、Z 或 ±HH:MM。仅日期按零点；未指定时区按 UTC+08:00。必须来自任务时间、用户指定时间或已知事实。 |
| `years`、`months` | 可选严格整数；正数增加、负数减少，省略/null按0；合并年月偏移后调整月末。 |
| `days`、`hours`、`minutes`、`seconds` | 可选严格整数，随后叠加；日为自然日，省略/null按0。 |

计算先合并年/月并定位目标月份，没有原日期时取月末，再叠加日时分秒。因此2026-01-31加1个月为2026-02-28；2024-02-29加1年同时减12个月仍为原日期。显式提供的时区偏移保持不变，不支持IANA时区/DST规则。

示例调用 `{"start_time":"2026-01-31","months":1}` 返回：

```json
{
  "start_time": "2026-01-31T00:00:00+08:00",
  "result_time": "2026-02-28T00:00:00+08:00",
  "weekday": "星期六",
  "month_end_clamped": true
}
```

`month_end_clamped` 表示年月阶段是否发生月末截断，后续加天数不改变该标记。无偏移返回原时间。日期无效、额外字段、非整数偏移在参数校验阶段拒绝；溢出公元1—9999年或timedelta范围时返回失败，不能当成成功日期。执行失败沿用有限纠错，成功后清除失败轨迹、保留完整私有审计。

该工具不读取当前时钟，不计算工作日/节假日或判断业务期限口径。时间注入与“今天/昨天/上周”的解释见[任务时间](context-rendering.md#任务时间与相对日期)。

---

## 提交、审计与展示

[MemoryJournal / NodeJournal](../../../../app/agent/core/journal.py)分别用于纯内存宿主和真实节点仓库。完整原生响应、推理及 raw_chunks 每次追加保存，无 K 轮窗口；新增 GenerationPayload.purpose 区分主生成、历史摘要、当前上下文整理及工作区整理，仅是私有 JSON 元数据，无需新增 SQL 列。

模型生成先于工具校验记录，因此拒绝的响应也保留。断流或取消时，底层[MLLM 客户端](../../../../app/infrastructure/mllm.py)的内部 on_raw_chunk 回调保存已接收的完整原始块。只在动作验收及提交成功后发布公开输出，原生推理不公开。网络尚未收到的数据或数据库不可用时不能保证持久化，调用方可从 AgentCoreError.journal 取得运行期记录。

已接受的 assistant/tool 对写入 TaskContent.exchanges；历史任务从该权威轨迹恢复原生消息，先验收完整配对，再按终态过滤；私有推理不读取也不重复注入。没有原生工具对的显式历史只展示真实公开输出。结构化校验回执 journal.audit 保存在运行期及实验产物中，未新增独立审计日志表；NodeJournal 持久化的是不可变原始生成、公开消息、已接受轨迹与工作区。

输出回调 `publish(kind, text)` 只在数据库提交确认后触发。最终消息已成功保存但回调失败时，返回 delivery_error_type，不重新生成或撤销完成状态；中途交付失败停止后续执行。输出回调取消也不能把已完成节点改为取消。

图像页面由[独立展示窗口](image-reading.md)在已提交 tool 回执之后按需解析。整体计数与生成使用相同多模态请求；整理保留仍有效的图片对应工具对，硬上限无法满足时停止。持久化只保存轻量查看记录，历史不会自动重新注入 PNG。

---

## 生命周期与故障

CoreSettings 默认最多 64 个主模型轮次、连续错误 3 次、成功整理最多 8 次、清理 5 秒。不设置整轮任务的总运行时间限制，Core循环及树服务的上下文加载、摘要准备均不添加累计截止时间；探索与助理模式统一遵循此规则。单次模型、HTTP和数据库请求仍使用各自超时，用户停止、调整方向和停机取消继续生效。运行选项由服务端构造；文件池容量和页面存活轮次已接入[运行配置](image-reading.md#运行配置)，模式工厂缺省加载，底层 Core 接收显式 options。生成不自动重试；提交异常不能视为参数错误并重放。

外部传入的模型客户端由调用方关闭；Core 自建客户端在成功、失败或取消后关闭。模型限流与原生分词复用已有基础设施。取消、单次依赖请求超时、模型或存储故障尽力封闭失败/取消节点；若封闭依赖本身失败，保留运行期错误而不声称已落盘。

不自动恢复或重放有生成记录的节点。TreeAgentResult 返回 tree_id、node_id、可选 summary_id 和 AgentResult，工程标识只用于程序和接口，不注入模型。并发同树写入依赖 revision 冲突失败，不自动迁移到另一分支或覆盖别人的状态。

用户停止与方向调整使用[节点生命周期控制](node-lifecycle.md)。外部终态先提交时，NodeJournal 保存迟到的原生响应至私有审计，Core 在执行其工具前停止；终态不接受公开输出或工作区变更。生成服务还负责取消本实例生产协程及有界等待，停机前需先 close 服务再关闭存储。

---

## 验证

2026-10-23 时间对齐：新增[时间专项](../../../../tests/test_agent_time.py)，相关189项回归中176项通过、13项需要独立MySQL的测试跳过。覆盖节点时间绑定、历史/当前/整理消息、时区、年月末与闰年、错误清理、实际HTTP工具Schema及模式装配；模型与数据库使用隔离替身，本轮未进行真实MLLM或实库验收。详见[验证记录](../../../../experiment/agent-time/20261023-alignment/analysis.md)。

[核心测试](../../../../tests/test_agent_core.py)、[预算与传输测试](../../../../tests/test_agent_context_budget.py)和[隔离实库组合测试](../../../../tests/test_question_agent_mysql.py)覆盖正常输出、纠错清理、完整推理、对象字符串兼容、重复键、引用、保护目标、自动和显式整理、总轮数及容量边界、客户端生命周期、故障与取消、数据库回查及历史分支。

真实模型实验使用合成资料，原始产物与失败分析分别保留在 experiment/agent-core 下，不访问共享业务库、钉钉或中央登录，不代表完整问答 HTTP 接口或生产性能验收。

2026-10-21 初次实现验证：完整 332 项通过，无跳过，其中 53 项为独立临时 MySQL/MyVector 验证；本次新增 29 项核心/预算/传输及 8 项组合实库测试。`pip check` 无冲突，本次文档 106 个相对文件链接有效。真实模型工作区、输出与历史连续任务通过，整理回执修正后亦通过，见[最终整理样例](../../../../experiment/agent-core/20261021-compaction-receipt/analysis.md)及其历史对照。未改实际 .env、共享业务库或其他项目。

专家的完整参数、配置、会话上限与原生响应私有落盘见[外部专家问答](external-expert.md)。

网页工具及 `publish_progress` 程序侧动作回调见[网页检索专题](web-search.md)，状态不作为权威业务输出。

子管理平台目录的字段、实时查询与折叠生命周期见[子管理平台查看](sub-management-platform.md)。


---

## 注册中心文件查询

`search_files`通过独立子智能体组合名称、摘要及AND/OR；`view_file_candidates`分页展示文件ID、名称和来源平台代码，正文可折叠。工具动作分别为`local-search`和`thinking`，子智能体生成及反馈以`file_retrieval`存入私有记录。数据授权、索引和生命周期见[文件查询](file-retrieval.md)。


---

## 注册中心文件图像查看

`view_registry_file(file_id, page_number?)`与用户附件查看共用图像渲染和折叠协议，文件ID是注册中心正式资源整数的字符串。下载凭据只由宿主绑定；每次页面读取重新远程授权，状态为`thinking`。详见[原件查看契约](registry-file-reading.md)。


---

## 公开正文流式交付

Core 仅提供可选 `OutputObserver` 接口，不构造 SSE 事件或展示 ID。探索模式在 [output_stream.py](../../../../app/agent/exploration/output_stream.py) 实现 `InteractionOutputStream`，由探索服务工厂装配；服务按任务创建观察器并传入 `core.run(output_observer=...)`。它接收累计工具分片，仅向宿主 `publish_stream(event, data)` 推送注册为公开输出的工具正文。助理工厂不装配观察器，正常任务只返回 `AgentResult.answer` 或通过最终交付回调返回一次完整回复；内部模型流式消费和私有推理保存仍保留。预览与 journal 的权威提交分离，成功输出沿用同一消息 ID；生成失败、校验拒绝及取消时收束预览，不把半成品追加到 accepted 交互。辅助摘要、工作区整理等子调用不接入公开正文流。HTTP 事件、重连、停止与历史恢复的唯一契约见[探索会话 SSE 与恢复](exploration-session.md#sse-与恢复)。
