# 工具注册、返回与自动折叠契约

## 用途与实现入口

公共 Core 统一处理工具声明、执行结果、页面解析和模型上下文，图像工具与后续文本、表格工具共用同一条链路。参考 contract-service-be 的 `ordinary/foldable` 内容模型及 `succeeded/failed/unknown` 执行状态；本项目保留树状历史、节点事务和完整原生生成，不引入线性 FIFO。

实现入口为[注册表](../../../../app/agent/core/tool/registry.py)、[结果模型](../../../../app/agent/core/tool/result.py)、[展示窗口](../../../../app/agent/core/page_display.py)与[内容渲染](../../../../app/agent/core/context_rendering/page.py)。图像文件的归属、游标和 LRU 见[图像阅读](image-reading.md)，树路径和摘要见[上下文管理](context-management.md)。

---

## 注册与宿主能力

`RegisteredTool` 将名称、描述、参数模型、纯预演 `handler`、可选 `parser`、`executor`、`return_types` 和 `page_resolver` 绑定为一项。参数顶层必须是对象，实际 Schema 的嵌套属性及动态属性须有说明；名称不能重复。

- `return_types` 是非空且不重复的元组，仅允许 `ordinary`、`foldable`，默认 `('ordinary',)`。
- 声明 `foldable` 必须提供执行器和页面解析器；注册时即拒绝缺失绑定。
- 宿主调用 `require_supported_content()` 检查整批工具。默认只声明普通能力；本项目 AgentCore 在构造、替换注册表和运行入口显式声明支持两种类型，在模型调用和状态提交前完成检查。
- 执行结果须通过统一模型校验，成功返回的内容类型必须属于该工具声明。仅声明可折叠成功结果的工具仍可返回普通类型的失败反馈。

注册声明属于程序契约，不作为模型函数参数。公共 Core 在既有六个基本工具之前注册 calculate_date 和 get_current_time，八项均按统一参数与执行契约处理；其余工具的相对顺序和参数 Schema 不变。

Core 的直接实现与必须继承的工具契约分开维护，见[工具分类](package-structure.md#工具分工与依赖)。主循环必须显式装配模式 finish_task；抽象基类不能实例化，缺失最终输出、错误输出类别或未继承参数契约均在模型调用前拒绝。ToolRegistry 本身允许独立工具集合，是否可运行主循环由 require_mode_tools 检查。

输出注册项可额外声明 `deliver_output(arguments) -> bool`，只有输出工具可使用。false 仍提交正文和终态，但不调用宿主交付回调；条件交付工具不接受流式预览，避免完整参数确定前泄露静默输出。当前由[助理 finish_task](../../../capability/application/assistant-consumption.md#finish_task)使用；探索输出未改变。

---

## 执行结果与失败边界

所有执行器返回 `ToolExecutionResult` 或严格符合其字段的字典；需要本地提交后更新游标时，使用 `ToolExecution(result, after_commit=...)`。不再接收任意 JSON 并自动包装为成功。

`after_commit` 可同步返回或返回 awaitable，宿主在权威提交成功后调用并等待，随后才继续下一次模型请求。提交失败不运行回调。发送类业务先将交付请求与节点事务一起保存，再在此回调尝试外部发送；不能在纯预演中发送消息。

| 字段 | 契约 |
| --- | --- |
| `status` | `succeeded`、`failed`、`unknown`，独立于内容类别。 |
| `content` | 缺省普通 `ordinary`；可折叠为 `foldable` 加非空 `pages`。 |
| `tool_result` | 模型允许看到的轻量结果对象，拒绝非有限数值；不包含页面正文、Base64 或私有状态。 |
| `system_guidence` | 按顺序保留的可信程序提示，来源为 `tool` 或 `workspace`，消息严格限定为非空 `user`。 |

```python
return ToolExecutionResult(
    status='succeeded',
    tool_result={'status': 'accepted', 'result': {'text': '简短资料', 'source': '业务来源'}},
)
```

`failed` 只进入连续纠错临时轨迹，不提交候选工作区、回调或页面；下一次完整成功后清理整段失败轨迹。`unknown`、执行异常、执行后的结果契约错误均停止，不自动重放，也不声称未发生副作用。原始模型生成保存在私有审计。当前扩展执行器用于读取资料，不由这套结果模型承诺外部写入与本地数据库的原子事务。

`succeeded` 表示执行器返回成功，宿主仍须完成结果验收和 journal 提交。只有提交成功才执行并等待 `after_commit` 回调、登记页面、接受工具对及发布公开输出。提交故障直接停止。失败或不确定结果只能是普通内容，不能附带提交回调。

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

计数和生成共用包含真实页面的同一份 messages。整理保留仍需展示的完整工具对；整理后仍超硬上限则停止，不通过丢弃未读页面绕过计数。成功工具返回的全部程序提示按顺序计入请求，封闭历史时移除程序操作提示。最近祖先摘要、分支路径、所选节点工作区、失败清理和完整私有推理契约继续使用现有实现。

[公共契约测试](../../../../tests/test_tool_content_contract.py)覆盖注册能力、严格结果、不确定状态、文本/表格/图片展示、重复引用、到期重开、提示顺序、真实请求计数和轻量轨迹。[图像测试](../../../../tests/test_file_viewer.py)另覆盖引用防篡改、跨查看器隔离及重新授权；[隔离数据库测试](../../../../tests/test_image_tool_mysql.py)检查落盘和历史恢复。

历史 483 项回归只证明当时图片专用实现，不作为公共返回契约已完成的证明；本次验收结果在完成后单独记录。


2026-10-22 本次修正验收：完整 502 项通过、无跳过，含 97 项独立 MySQL/MyVector 测试；新增 17 项公共契约与 2 项引用隔离用例，pip check 通过。第一次回归的验证日志占用 tests/tmp 导致 1 项清理错误，调整记录位置后完整重跑通过，详见[回归记录](../../../../experiment/tool-content-contract/20261022-115012-regression/analysis.md)。

[实际折叠样例](../../../../experiment/image-tool/20261022-115000-foldable/model-visible.md)使用真实 Core 和合成 PDF，图片数量依次为 0、1、1、0、1；两次打开同页具有不同私有展示标识。模型和计数使用替身，不代表真实模型理解验收；原始请求、结果与分析见[样例分析](../../../../experiment/image-tool/20261022-115000-foldable/analysis.md)。

网页搜索及正文页也按本契约执行，分页游标通过 `after_commit` 推进；重新打开采用独立签名展示标识。所有注册项必须具有 `ToolProgress`，默认 `DEFAULT_TOOL_PROGRESS`（`thinking` / 正在思考），不接受 `None`。工作区、上下文整理、中途/最终输出和图像阅读工具继承该默认声明；网页搜索与正文读取覆盖为 `online-search`，列表查看统一为 `thinking`，专家工具覆盖为 `external-expert`。动态 `progress_factory` 可同步返回 `ToolProgress`，也可异步完成授权资料读取后返回；例如注册中心文件查看仅展示已授权的入库 `name`。参数校验通过后，注册表在执行前发布动作，执行结束、异常或取消时恢复默认思考；无发布回调的内部调用仍正常执行。动作供宿主展示当前操作，不进入模型 Schema、公开消息或权威工作区。见[网页工具](web-search.md)和[注册中心文件查看](registry-file-reading.md)。


网页临时图片被 LRU 释放时，解析器可抛出明确的 `ToolPageUnavailable`。展示窗口仅将对应资料标记为“当前无法读取，需要时请重新打开来源”，保留其他可用资料和轻量引用；不使用占位图伪造原图，不把普通异常或媒体类型错误当成资料过期。展示机会照常消耗，图片释放不会重置图像存活轮次。该标记只影响当前渲染，不回写历史工具回执。图像任务生命周期见[网页图片筛选](web-search.md#网页图片筛选与任务内存)。
