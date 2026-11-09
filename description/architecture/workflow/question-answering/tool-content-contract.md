# 工具注册、返回与自动折叠契约

## 用途与实现入口

公共 Core 统一处理工具声明、执行结果、页面解析和模型上下文，图像工具与后续文本、表格工具共用同一条链路。参考 contract-service-be 的 `ordinary/foldable` 内容模型及 `succeeded/failed/unknown` 执行状态；本项目保留树状历史、节点事务和完整原生生成，仅对当前选定历史路径执行 FIFO 预算管理，不将树结构改为全局线性历史。

实现入口为[注册表](../../../../app/agent/core/tool/registry.py)、[结果模型](../../../../app/agent/core/tool/result.py)、[展示窗口](../../../../app/agent/core/page_display.py)与[内容渲染](../../../../app/agent/core/context_rendering/page.py)。图像文件的归属、游标和 LRU 见[图像阅读](image-reading.md)，树路径和摘要见[上下文管理](context-management.md)。

---

## Core 工具反馈迁移清单

以下按 Core 包实际注册实现梳理，共 20 个具体工具，另有模式必须实现的 `finish_task` 抽象契约。可选工具由宿主装配，不代表每次请求都启用。`emit_progress` 在探索模式注册，个人助理专属工具与内部子智能体工具另行迁移，不计入此清单。

| 优先顺序 | 工具 | 状态与主要检查点 |
| --- | --- | --- |
| 1 | `get_current_time` | 已改：独立末尾渲染钩子，保留结构化时间；验证两种模式下一轮正文、保存回执和参数纠错清理。 |
| 2 | `calculate_date` | 已改：独立末尾钩子渲染成功与溢出失败，保留计算字段；验证月末截断、两种模式实际反馈、连续纠错清理与成功回执保存。 |
| 3 | `workspace_add`、`workspace_replace`、`workspace_delete` | 已改：各自注册末尾钩子，使用宿主已计算的容量反馈；验证两种模式的实际正文、缺失路径纠错清理及提交失败不发送成功回执。 |
| 4 | `compact_context` | 已改外层及内部工具反馈：宿主回执展示整理任务数量；内部工作区编辑与主题选择分别注册末尾钩子，完成工具直接终止，正文 JSON Schema 生成保留原协议。验证范围见主题整理专题。 |
| 5 | `search_memory`、`search_files` | 两者外层及内部工具已接入独立钩子，覆盖条件/候选反馈、参数纠错、命中/空结果/依赖失败与结果集提交。 |
| 6 | `view_memory_query`、`view_file_candidates`、`view_image_file`、`view_registry_file` | 已改：三态轻量回执、定向失败反馈；保留资料业务 ID、翻页提交及隐藏历史恢复，图像展示期失效转为不可读状态。 |
| 7 | `search_web`、`view_web_search_results`、`open_web_page` | 已改：搜索成功/空结果/失败、搜索资料三态、正文与无正文提示；精炼子模型不使用函数工具，保留结构化生成协议。 |
| 8 | `list_sub_management_platforms` | 已改：目录三态、空目录及查询失败；仍包含离线平台，平台资料不重复塞进回执。 |
| 9 | `ask_external_expert`、`continue_external_expert` | 已改：直接展示已有专家答复，保留会话标识、轮次和全文；错误为可读反馈。 |
| 10 | `broadcast_platform_a2a`、`continue_platform_a2a` | 已改：直接展示已有协作反馈，保留响应 ID、超时续等和不重复发送语义；unknown 仍终止。 |
| 单独检查 | `finish_task` 抽象契约及模式实现 | 已改：业务钩子之后追加反馈专用钩子，历史回执仅确认正文保存，不确认外部送达；本轮无下一轮成功反馈。 |

现有 `ToolRegistry` 按注册项依次执行钩子，成功、失败和未知结果均可经过钩子；执行器抛出异常时不运行后续钩子。每次迁移只为目标工具注册末尾渲染钩子，不能将成功业务钩子无条件用于失败路径。设计与验收以[工具反馈渲染规范](../../../standard/tool-feedback.md)为准。

Core 清单中的 20 个具体工具及 `finish_task` 历史回执已完成迁移。内部文件检索、记忆检索、主题规划和工作区精简工具按各自专题验收；网页精炼的无工具结构化生成、外部服务托管的联网能力不改为本地函数反馈。探索专属 `emit_progress` 和个人助理专属工具不在此清单，不能将 Core 完成理解为全项目完成。

公共 [feedback.py](../../../../app/agent/core/tool/feedback.py) 仅包装业务钩子提供的普通或三态正文，不集中存储业务提示。业务失败原因仍在产生错误的分支维护。旧 JSON 回退仅保留用于旧数据及兼容注册项；新增工具不得沿用。

本轮 [剩余注册项验收](../../../../tests/test_remaining_core_feedback.py) 覆盖两种模式下一轮实际成功/失败消息、纠错清理、回调和结构化结果保留、三态切换、最终正文不被反馈钩子改写，以及探索流式观察器兼容。配合网页无正文、平台协作服务、既有检索与查看测试，共 109 项通过；模型、HTTP 和业务依赖使用替身，不代表真实外部服务联调。

---

## 模式专属工具反馈清单

探索的 `emit_progress`、个人助理的以下 19 个专属工具均已接入各自的末尾渲染钩子；两种模式的 `finish_task` 继承 Core 反馈专用钩子。函数工具参数继续采用原有 Schema，最终答案的钉钉转换仍是独立业务钩子。

| 工具 | 反馈及关键边界 |
| --- | --- |
| `search_assistants`、`view_assistant_query`、`send_assistant_message` | 查询、空结果、截断、目录三态和发送登记；发送成功不代表收到或处理。 |
| `search_memory_window` | 相邻任务结果集 ID、数量和页数，空结果与定向失败；仍可直接传已知任务 ID。 |
| `search_calendar_events`、`view_calendar_query` | 带时区窗口、截断与空结果、日程三态；查询失败或空结果不推断员工闲忙。 |
| `search_portraits`、`view_portrait_query` | 查询 ID、用户/偏好数量、截断及画像三态；页码越界和失效反馈由查询池就近提供。 |
| `add_portrait`、`delete_portrait` | 稳定 key、新增/删除或无需删除；同事务消费独立结构化结果，保留旧 JSON 历史兼容。 |
| `list_reminders`、`create_reminder`、`delete_reminder` | 列表三态、空页、offset、提醒 ID 和带时区排期；已取消再次删除明确无需重复；创建结果独立提交。 |
| `search_file_notes`、`view_file_note_query` | 名称/摘要独立 AND/OR、时间及旧结果范围筛选，任务 LRU、截断继承、空结果、失效提示及查看三态。 |
| `add_file_note`、`delete_file_note` | 新增、重复拒绝、删除与无需删除；独立事务，结果不确定时终止，不反解析回执写入。 |
| `get_file_visibility`、`set_file_visibility` | 查询、修改、无变化、权限及依赖失败；保留上传时间，未知结果仍终止，无下一轮模型请求。 |

以上工具没有新增内部子智能体循环；共用 Core 的检索、上下文整理及最终转换边界沿用各自专题。[助理剩余工具反馈测试](../../../../tests/test_assistant_remaining_feedback.py)验证实际下一轮消息、失败清理、结构化字段与回调保留、隐藏/不可读以及无变化分支。[结构化结果持久化测试](../../../../tests/test_tool_result_persistence.py)使用随机临时 MySQL 库验证真实 Core、节点检查点与提醒/画像事务消费、冷恢复、幂等重放、冲突及回滚，不访问业务数据、不调用真实模型或发送消息。

2026-11-09 本次整组回归 119 项全部通过，包含显式启用的文件可见范围、画像事务和新结构化回执隔离 MySQL 验证；无跳过项。测试仅使用随机临时库和模型/外部服务替身，未修改业务数据、启动迁移或发送真实员工消息。

---

## 可折叠工具的可读回执

`FoldableToolContent.feedback` 可选提供 `FoldableReceiptText`，三个必填非空字段为 `visible`（正文可展示）、`hidden`（正文未展示）、`unavailable`（至少一页资料无法读取）。工具独立末尾钩子生成全部文案；公共渲染器仅按真实展示状态选取文本，不维护业务提示。未提供 feedback 的工具继续沿用原 JSON 回退。

成功历史保存 `hidden` 文本；活跃展示窗口注入资料时选择 `visible`，解析器报告 `ToolPageUnavailable` 时选择 `unavailable`，展示到期后回到保存的隐藏回执。页面引用、正文解析、多模态注入、计数、签名和提交回调不变。可读回执不能从持久化正文反解析恢复临时页面；历史恢复不自动重新打开资料。

---

## 注册与宿主能力

> **规范与兼容现状：** 模型可见正文必须遵循[工具反馈渲染规范](../../../standard/tool-feedback.md)。本文所述普通 JSON 回退及可折叠 JSON 回执是现有兼容机制，尚待按工具逐项迁移，不表示它们符合新的展示要求；内部结构化字段和持久化消费契约仍须保留，不能仅替换字符串破坏业务提交。

`RegisteredTool` 将名称、描述、参数模型、纯预演 `handler`、可选 `parser`、`executor`、`return_types`、`page_resolver` 和独立 `hooks` 元组绑定为一项。参数顶层必须是对象，实际 Schema 的嵌套属性及动态属性须有说明；名称不能重复。

- `return_types` 是非空且不重复的元组，仅允许 `ordinary`、`foldable`，默认 `('ordinary',)`。
- 声明 `foldable` 必须提供执行器和页面解析器；注册时即拒绝缺失绑定。
- 宿主调用 `require_supported_content()` 检查整批工具。默认只声明普通能力；本项目 AgentCore 在构造、替换注册表和运行入口显式声明支持两种类型，在模型调用和状态提交前完成检查。
- 执行结果须通过统一模型校验，成功返回的内容类型必须属于该工具声明。仅声明可折叠成功结果的工具仍可返回普通类型的失败反馈。

注册声明属于程序契约，不作为模型函数参数。公共 Core 提供六个直接实现（日期、当前时间、三个工作区工具、上下文整理），加上必需的模式 finish_task 共七项；探索另注册 emit_progress，共八项。宿主可继续注入可选工具。

Core 的直接实现与必须继承的工具契约分开维护，见[工具分类](package-structure.md#工具分工与依赖)。主循环必须显式装配模式 finish_task；抽象基类不能实例化，缺失最终输出、错误输出类别或未继承参数契约均在模型调用前拒绝。ToolRegistry 本身允许独立工具集合，是否可运行主循环由 require_mode_tools 检查。

输出注册项可额外声明 `deliver_output(arguments) -> bool`，只有输出工具可使用。false 仍提交正文和终态，但不调用宿主交付回调；条件交付工具不接受流式预览，避免完整参数确定前泄露静默输出。当前由[助理 finish_task](../../../capability/application/assistant-consumption.md#finish_task)使用；探索输出未改变。

---

## 执行结果与失败边界

所有执行器返回 `ToolExecutionResult` 或严格符合其字段的字典；需要本地提交后更新游标时，使用 `ToolExecution(result, after_commit=...)`。成功输出工具还可通过 `output` 携带最终正文；[工具执行后钩子](agent-core.md#工具执行后钩子)在执行结果校验后、权威提交前按该工具自身注册顺序运行，钩子之间及不同工具之间的边界由注册表维护。不再接收任意 JSON 并自动包装为成功。

`after_commit` 可同步返回或返回 awaitable，宿主在权威提交成功后调用并等待，随后才继续下一次模型请求。提交失败不运行回调。发送类业务先将交付请求与节点事务一起保存，再在此回调尝试外部发送；不能在纯预演中发送消息。

| 字段 | 契约 |
| --- | --- |
| `status` | `succeeded`、`failed`、`unknown`，独立于内容类别。 |
| `content` | 缺省普通 `ordinary`，可选非空 `text` 作为轻量反馈正文；可折叠为 `foldable` 加非空 `pages`，可选 `feedback` 提供三态回执。 |
| `tool_result` | 结构化轻量结果对象，拒绝非有限数值；默认展示为 JSON，普通内容提供 `text` 或可折叠内容提供 `feedback` 时展示对应正文。不包含页面正文、Base64 或私有状态。 |
| `system_guidence` | 按顺序保留的可信程序提示，来源为 `tool` 或 `workspace`，消息严格限定为非空 `user`。 |

```python
return ToolExecutionResult(
    status='succeeded',
    tool_result={'status': 'accepted', 'result': {'text': '简短资料', 'source': '业务来源'}},
)
```

普通结果需要友好排版时，由业务工具在结果分支就地渲染 `OrdinaryToolContent(text=...)`。公共渲染器直接将该文本作为 `role=tool` 的 `content`，不再包一层 JSON；未提供 text 的工具与可折叠结果保持原格式。text 必须为非空白字符串，属于轻量结果，不用于大容量资料。它不改变执行状态、纠错、页面或提交语义。具体原因与修正动作仍维护在业务校验分支附近，公共层不承载业务文案表。

提供 text 的成功工具对保存同一份可读正文，后续历史原样恢复；其结构化 `tool_result` 不额外写入这条历史回执。依赖机器回执执行事务钩子的工具不能直接切换展示而忽略持久化契约。文件可见范围工具使用独立元信息事务，不从工具历史解析字段执行更新，已接入该能力。

`failed` 只进入连续纠错临时轨迹，不提交候选工作区、回调或页面；下一次完整成功后清理整段失败轨迹。`unknown`、执行异常、执行后的结果契约错误均停止，不自动重放，也不声称未发生副作用。原始模型生成保存在私有审计。工具也可封装发送、提醒和画像变更；这套结果模型不自动承诺外部写入与本地数据库的原子事务，具体副作用须遵守所属工具的提交和幂等契约。

`succeeded` 表示执行器返回成功，宿主仍须完成结果验收、该工具全部钩子及 journal 提交。只有提交成功才执行并等待 `after_commit` 回调、登记页面、接受工具对及发布公开输出。提交故障直接停止。失败或不确定结果只能是普通内容，不能附带提交回调。

---

## 页面引用与自动折叠

每个 `ToolPageReference` 包含 `resource_id`、`display_id`、`locator`、`media_type`、`description`。媒体类型支持 `text`、`table`、`image`；同次返回可有多个页面，展示标识必须唯一。重新打开同一页必须产生新 `display_id`，不能借用旧展示机会。

1. 工具返回轻量页面引用，宿主先验收引用与唯一性，再提交隐藏占位回执。
2. 下一次请求由注册表按工具绑定的解析器取得实际内容；图片须为 `image_url`，文本和表格须为非空 `text`。不匹配、空内容、无对应引用或缺失原始工具回执时停止。
3. 正文紧随所属 assistant/tool 对，作为独立资料消息展示；统一程序提醒使用 `system-guidence` 格式。参数和页面文字不能提升为系统角色。
4. 默认供 5 次完整主模型响应使用，可通过 [AGENT_PAGE_DISPLAY_ROUNDS](image-reading.md#运行配置)调整。`stop` / `tool_calls` 消耗机会，包括协议错误和工具失败；计数、整理、截断及网络失败不消费。
5. 到期后停止注入正文，原始工具对保留业务来源、位置和未展示说明。再次读取必须重新调用工具。普通结果不进入页面窗口。

内部 `resource_id` / `display_id` 保留在运行期程序对象，不自动复制给模型或持久化回执；模型只看到允许展示的 `description`、`locator`、`media_type` 与业务结果。业务文件 UUID 由图像工具在来源和结果中显式提供。缓存、剩余展示轮次和工程编号不进入模型文本，符合本项目提示词规范；这点有意不同于参考项目的完整元信息展示。

`TaskContent.exchanges` 保存轻量隐藏回执及已接受工具对，不保存临时正文、PNG、Base64 或解析器。历史恢复不会自动重开资源；后续工具调用按真实归属重新读取。运行结束释放页面引用，文件模型池的生命周期独立。

---

## 上下文、容量与验证

计数和生成共用包含真实页面的同一份 messages。整理保留仍需展示的完整工具对；整理后仍超硬上限则停止，不通过丢弃未读页面绕过计数。成功工具返回的全部程序提示按顺序计入请求，封闭历史时移除程序操作提示。上下文与工作区按模式恢复：探索使用选定路径和节点快照，助理使用持久化压缩窗口和当前工作区；失败清理和完整私有推理契约继续使用现有实现。

[公共契约测试](../../../../tests/test_tool_content_contract.py)覆盖注册能力、严格结果、不确定状态、文本/表格/图片展示、重复引用、到期重开、提示顺序、真实请求计数和轻量轨迹。[图像测试](../../../../tests/test_file_viewer.py)另覆盖引用防篡改、跨查看器隔离及重新授权；[隔离数据库测试](../../../../tests/test_image_tool_mysql.py)检查落盘和历史恢复。

历史 483 项回归只证明当时图片专用实现，不作为公共返回契约已完成的证明；公共返回契约的历史验收另列如下，不代表当前完整回归结果。


2026-10-22 本次修正验收：完整 502 项通过、无跳过，含 97 项独立 MySQL/MyVector 测试；新增 17 项公共契约与 2 项引用隔离用例，pip check 通过。第一次回归的验证日志占用 tests/tmp 导致 1 项清理错误，调整记录位置后完整重跑通过，详见[回归记录](../../../../experiment/tool-content-contract/20261022-115012-regression/analysis.md)。

[实际折叠样例](../../../../experiment/image-tool/20261022-115000-foldable/model-visible.md)使用真实 Core 和合成 PDF，图片数量依次为 0、1、1、0、1；两次打开同页具有不同私有展示标识。模型和计数使用替身，不代表真实模型理解验收；原始请求、结果与分析见[样例分析](../../../../experiment/image-tool/20261022-115000-foldable/analysis.md)。

网页搜索及正文页也按本契约执行，分页游标通过 `after_commit` 推进；重新打开采用独立签名展示标识。所有注册项必须具有 `ToolProgress`，默认 `DEFAULT_TOOL_PROGRESS`（`thinking` / 正在思考），不接受 `None`。工作区、上下文整理、中途/最终输出和图像阅读工具继承该默认声明；网页搜索与正文读取覆盖为 `online-search`，列表查看统一为 `thinking`，专家工具覆盖为 `external-expert`。动态 `progress_factory` 可同步返回 `ToolProgress`，也可异步完成授权资料读取后返回；例如注册中心文件查看仅展示已授权的入库 `name`。参数校验通过后，注册表在执行前发布动作，执行结束、异常或取消时恢复默认思考；无发布回调的内部调用仍正常执行。动作供宿主展示当前操作，不进入模型 Schema、公开消息或权威工作区。见[网页工具](web-search.md)和[注册中心文件查看](registry-file-reading.md)。


网页临时图片被 LRU 释放时，解析器可抛出明确的 `ToolPageUnavailable`。展示窗口仅将对应资料标记为“当前无法读取，需要时请重新打开来源”，保留其他可用资料和轻量引用；不使用占位图伪造原图，不把普通异常或媒体类型错误当成资料过期。展示机会照常消耗，图片释放不会重置图像存活轮次。该标记只影响当前渲染，不回写历史工具回执。图像任务生命周期见[网页图片筛选](web-search.md#网页图片筛选与任务内存)。
