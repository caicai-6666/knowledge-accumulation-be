# 助理最终正文转换

## 用途与入口

将个人助理 `finish_task.content` 转换为钉钉 `sampleMarkdown` 正文。转换作为助理 `finish_task` 自身的执行后钩子，在结果提交前完成；主助理自行组织答案，转换器只承担渠道展示适配。静默完成、助理间通信、提醒入队、探索模式及入站拒绝反馈不调用此转换器。

单次生成实现位于 [message_rendering](../../../../app/agent/assistant/message_rendering/agent.py)，[工具钩子](../../../../app/agent/assistant/message_rendering/hook.py)负责调用转换器并替换工具输出，Core 的[独立钩子链](agent-core.md#工具执行后钩子)负责顺序执行与验收。发送和确认时序见[助理交付](../../../capability/application/assistant-consumption.md#最终正文渲染钩子)。

---

## 上下文与输出

每次请求只包含一条 system 和一条 user 消息，不带历史、员工背景、主助理工作区、工具、失败轨迹或运行标识。

| 消息 | 内容与顺序 |
| --- | --- |
| system | `# 任务描述`：输入输出、保真要求、失败边界和示例；随后 `# 钉钉规则`：允许的语法与渠道排版规则。 |
| user | `## 用户输入` → `### 原始答复` → 自适应 text 围栏中的原始 `finish_task.content`。 |

[提示词与消息构建](../../../../app/agent/assistant/message_rendering/prompt.py)复用已有 `text_block()`；围栏长度超过原文连续反引号，原文逐字保留，不把其中的指令提升为系统规则。`send_to_user` 由主流程处理，不重复作为转换资料；任务 ID、用户 ID 和审计关联只留在程序侧。排版风格沿用[上下文渲染规范](context-rendering.md)。

采用单次非流式生成，通过提示要求合法 JSON，不向服务端发送 `response_format`（包括 `json_schema` 和 `json_object`）；[唯一输出字段](../../../../app/agent/assistant/message_rendering/schema.py)为：

```json
{"content":"转换后的完整钉钉 Markdown 正文"}
```

本地 Pydantic 模型要求 `content` 是非空字符串，禁止额外字段；同时拒绝纯空白、重复 JSON 键、非标准数值、JSON 围栏、拒绝响应、工具调用和截断响应。原生 `reasoning` / `reasoning_content` 不放入 JSON Schema 或钉钉正文，完整保存在私有生成记录中。

每次 `render` 最多调用一次生成，不进行内部重试、多轮纠错或二次总结。失败没有下一轮模型请求，安全定向原因沿 Core 错误返回宿主，不能声称已通知模型纠错。无效结果、取消或审计失败终止任务，不提交原文、不继续后续钩子，也不自动重放主助理。

---

## 钉钉规则与保真边界

官方依据为[机器人发送消息类型：Markdown 支持的语法](https://open.dingtalk.com/document/orgapp/types-of-messages-sent-by-robots)。其明确针对 `sampleMarkdown` 列出一至六级标题、引用、加粗、斜体、链接、图片、有序及无序列表，并建议真实换行前后分别添加两个空格。

转换提示限定使用上述格式，明确 JSON 中的 `\n` 解码后必须是真实换行，并提供可直接解码的完整 JSON 示例；空格不能替代换行。段落及分组留空行，普通 `•` 条目转为 Markdown 列表，装饰横线改为章节标题；表格按行或对象纵向展开，保留字段对应关系。代码、JSON、公式和逐字引文内部的字符及必要空白属于内容，保留原样，仅可移除外围围栏。不得增加事实、来源、链接、图片、推测或承诺，不得为简短而删减正文。

上述保真和排版规则属于模型语义约束；程序验收结构与生成完整性，不以简单正则误删代码或引用，也不宣称结构校验能证明语义完全不变。实际模型转换质量和不同钉钉客户端展示仍需独立验收。

---

## 生成配置与资源

`DingTalkMessageRenderer(settings, thinking_effort='medium', client=None, counter=None)` 复用[MLLM 客户端](../../../capability/infrastructure/vllm-models.md)，默认 `enable_thinking=true`、`reasoning_effort=medium`；显式选择 `none` 关闭推理，此时不向分词和生成请求传递 `reasoning_effort`；其他采样参数、模型地址、输出额度与超时沿用显式传入的 MLLM 配置。档位按已有模型协议映射，不在提示词中添加推理档位或工具格式模板。

应用配置 `ASSISTANT_RENDERING_THINKING_EFFORT` 默认 `medium`，可选 `none / low / medium / xhigh`，与主助理 `ASSISTANT_THINKING_EFFORT` 独立。升级已有配置时需把该项显式改为 `medium`；旧文件中的 `none` 仍会关闭推理。配置在启动装配时读取，运行中的进程需重启才能生效。使用与生成相同配置调用原生分词，预留最大输出额度；超预算直接失败，不截断原文。分词的基础设施瞬时重试不计为第二次生成。

应用启用助理时装配转换器，将 `AssistantMessageRenderingHook(agent)` 只注册到助理 `finish_task`，后台消费者关闭后再关闭转换器。自建模型客户端由转换器关闭，外部注入客户端由宿主管理。`create_app(assistant_finish_hooks=...)` 可替换该工具的有序钩子元组；`None` 装配默认转换器，显式空元组表示禁用，注入资源由调用方管理。助理 `build_core/build_service(finish_hooks=...)` 及 `build_registry(finish_hooks=...)` 负责传递注册项；自定义应用 `assistant_agent_factory` 也接收 `finish_hooks`，必须按该接口继续装配。交付服务直接发送已经提交的正文，不调用模型。

---

## 私有审计与交付恢复

执行顺序为：参数验收 → 工具执行 → 本工具钩子逐个转换并验收 → 正常提交工具调用记录、节点答案和交付正文 → 原子抢占发送 → 钉钉 Markdown 接口。

钩子签名为 `await hook(arguments, execution, context) -> ToolExecution`，读取前一个钩子的 `execution.output`，转换完成后用 `dataclasses.replace(execution, output=content)` 返回；`send_to_user=false` 直接透传。实例只保存转换器，不缓存任务身份或正文；每次调用的 `ToolHookContext.record_generation` 绑定当前 journal，不与其他工具或并发任务共享调用状态。

转换模型的完整原生响应在解析前通过 `MemoryJournal.record_payload` 写入当前任务私有生成，`NodeJournal` 复用已有正常检查点保存，使用 `purpose=assistant_message_rendering`。记录助手消息、原生推理、原始响应、用量和结束原因；请求失败或取消记录状态及安全异常类别。没有收到完整响应时不伪造原始内容。生成记录的 `complete` 只代表收到响应，不代表正文通过验收。

Core 在所有钩子通过后，以最终 `output` 替换已接受参数的 `content`，重新校验参数，再沿普通提交路径同时保存 `finish_task` 调用记录和答案。助理现有提交逻辑从同一答案创建 `AssistantDelivery.message`，下一次通过原有树上下文构建读取的 `finish_task.content` 也是转换后的正文。原始主模型调用只保留在私有生成审计中。

不增加或修改问答树结构、仓库更新接口或 SQL 列，也不做提交后正文替换。工作区和 `send_to_user` 不由转换器修改。钩子失败时没有最终答案或用户交付记录；审计按现有失败封闭机制保留，不扫描终态生成来恢复发送正文。

最终提交成功后，发送重试直接使用交付账本内已转换的正文，无需重新转换。用户发送结果不明仍禁止自动重发。并发仍由现有每用户消费锁和原子发送抢占保障，不通过数据库事务持锁执行模型网络请求。

---

## 验证

[Core 钩子测试](../../../../tests/test_tool_hooks.py)覆盖不同工具与工厂实例隔离、注册列表快照、同步/异步顺序、并发审计隔离、输出和执行状态验收、失败/取消停止、提交回调顺序、静默与探索旁路、预览保护，并通过原有树上下文构建验证下一轮模型实际收到转换后的 `finish_task.content`。

[转换器测试](../../../../tests/test_assistant_message_rendering_agent.py)通过真实 `MLLMClient` 和 HTTP MockTransport 检查两条上下文、默认 medium 推理与显式关闭、分词/生成参数一致、请求不含 `response_format`、本地 Pydantic 验收、一次生成、异常审计与 Core 提交集成。[交付测试](../../../../tests/test_assistant_rendering.py)验证直接发送权威正文及原有抢占和不确定性；[生命周期测试](../../../../tests/test_robot_initialization_lifecycle.py)和[装配回归](../../../../tests/test_registry_service_identity.py)检查默认/自定义工厂钩子注入、档位及逆序关闭。

以上隔离测试使用内存、网络与应用资源替身，不访问业务数据库或向员工发送消息；在线模型实验另见下文，真实 SQL 往返及钉钉手机端效果尚未验收。

2026-10-30 验证：助理、Core 钩子、原生调用/参数纠错、应用装配及平台 A2A 相关回归共 100 项，93 项通过、7 项因未启用隔离 MySQL 环境跳过。变更源码语法、转换专题链接及 `git diff --check` 检查通过。测试未请求真实模型或钉钉，未修改真实 `.env`、业务数据库或重启服务。


2026-10-30 关闭转换推理（历史阶段，已由下文方案取代）：默认档位改为 `none`，本地实际配置同步为 `none`；只覆盖转换请求，主助理推理配置保持不变。更新转换器和生命周期测试后，相关 41 项隔离测试全部通过；实际分词及生成请求均为 `enable_thinking=false`，不发送 `reasoning_effort`，仍为单次非流式 JSON Schema 生成。两套 Compose 配置和差异格式校验通过。本次未重启服务、发送钉钉或再次调用真实模型。

关闭推理不代表内容保真问题已修复：此前在线对照曾出现关闭推理且保留 Schema 时仅返回开头一句的情况，证据见[转换诊断](../../../../experiment/assistant-rendering-diagnosis/20261030-ddr6-stream/analysis.md)。当前结构校验仍不能自动发现全部语义遗漏。

---

## 开启推理后的提示词实验

2026-10-30 使用脱敏长表格原文，对照生产基线提示、明确 JSON 换行转义的提示和重新组织的布局提示。实验显式开启原生推理 `medium`，单次请求超时放宽至 1800 秒；模型、采样参数和 Pydantic 输出 Schema 保持一致。该对照阶段的覆盖仅用于实验；后续采用方案见本节末尾。实验超时为 1800 秒，应用请求超时仍沿用 MLLM 配置。

| 提示或检查 | 观察结果 | 证据 |
| --- | --- | --- |
| 基线提示，流式采集 | 100.099 秒正常结束，1245 个推理 token；正文真实换行数为 0，排版检查失败。 | [基线分析](../../../../experiment/assistant-rendering-prompts/20261030-151917-baseline-long_table/analysis.md) |
| 明确 JSON `\n` 示例，流式采集 | 98.060 秒正常结束，1219 个推理 token；正文真实换行数仍为 0，排版检查失败。 | [转义提示分析](../../../../experiment/assistant-rendering-prompts/20261030-152057-escaped-long_table/analysis.md) |
| 重写布局提示，流式采集 | 80.443 秒发生接口错误，尚无正文；不能判定提示词效果。 | [布局提示分析](../../../../experiment/assistant-rendering-prompts/20261030-152235-layout-long_table/analysis.md) |
| Unicode 换行提示及布局提示，非流式复核 | 两次均在分词阶段连接失败，没有发起生成。 | [Unicode 提示记录](../../../../experiment/assistant-rendering-prompts/20261030-152419-unicode-long_table/analysis.md)、[布局复核记录](../../../../experiment/assistant-rendering-prompts/20261030-152420-layout-long_table/analysis.md) |
| 模型服务只读检查 | 健康检查和模型列表均返回连接错误。 | [服务检查](../../../../experiment/assistant-rendering-prompts/20261030-152443-service-check/analysis.md) |

结论限于以上样本：开启推理和放宽等待时间不足以修复换行，明确 JSON 转义示例也未在该样本奏效。结构化输出路径仍值得排查，但没有服务端日志，不能确定模型、模板或约束解码中的具体原因。该阶段尚未找到经验证有效的替代提示，未将实验候选替换到应用。有效候选仍需完成非流式、多类型输入和语义保真复核；本次未发送钉钉，也未验证客户端视觉效果。

15:28（北京时间）[重新探测](../../../../experiment/assistant-rendering-prompts/20261030-152834-service-check/analysis.md)时，健康与模型列表均返回 200，已继续生成实验。此前连接失败只描述对应时点，不表示服务持续关闭。探测脚本另修复了空密钥仍发送 Bearer 请求头的问题；该缺陷产生的 `LocalProtocolError` 不作为服务不可用证据，见[探测修正](../../../../experiment/assistant-rendering-prompts/20261030-152826-service-check/analysis.md)。

恢复连接后的真实非流式复测结果：

| 提示与接口约束 | 结果 | 证据 |
| --- | --- | --- |
| Unicode 换行提示，保留 Schema | 132.973 秒完成，0 个真实换行，换行位置出现异常字符；不可采用。 | [Unicode 复测](../../../../experiment/assistant-rendering-prompts/20261030-152900-unicode-long_table/analysis.md) |
| 布局提示，保留 Schema | 140.440 秒完成，0 个真实换行；不可采用。 | [布局复测](../../../../experiment/assistant-rendering-prompts/20261030-153113-layout-long_table/analysis.md) |
| 相同布局提示，仅去除接口 Schema | 104.500 秒完成，49 个换行、17 个列表项；结构、关键词和该样本人工逐段复核通过。 | [长文对照](../../../../experiment/assistant-rendering-prompts/20261030-153731-layout-long_table/analysis.md) |
| 短 Markdown 原样复制，去除接口 Schema | 7.264 秒完成，解码后与输入逐字一致，保留 3 个换行。 | [短文对照](../../../../experiment/assistant-rendering-prompts/20261030-153700-copy_probe-newline_probe/analysis.md) |

长文请求的实际参数仅 `response_format` 不同，仍开启 `medium`，仍由提示要求 JSON，并使用原本地结构验收。该对照支持优先检查当前服务的结构化约束路径，但不能定位 tokenizer、模板或约束解码的具体根因，也不是取消生产 Schema 的充分依据。对照请求有时间重叠，耗时仅为观察值，不作为严格性能基准。该对照阶段的应用请求契约保持不变；后续依据用户确认采用下述方案。

短文带 Schema 的请求最终自然结束：493.247 秒、16384 个生成 token、`finish_reason=length`，其中推理仅占 157 token，正文重复生成不可见字符，不能归为推理耗时过长或请求超时。[完整对照](../../../../experiment/assistant-rendering-prompts/20261030-153340-copy_probe-newline_probe/analysis.md)保留原始响应；去掉接口约束后的两次对照分别为 7.264 秒和 6.575 秒，均逐字保留短 Markdown。

---

## 当前采用方案

经需求方确认，转换器取消接口级 JSON Schema 约束，不切换到 `json_object` 模式；提示要求唯一 `content` 字段的合法 JSON，本地继续使用 `RenderedMessage` 和原严格解析器。原生推理默认为 `medium`，保留 `none` 显式关闭选项；主助理及其他 Agent 的 Schema 请求保持各自原有协议。

提示词明确 JSON 换行转义、完整保留表格字段与末段，并只转换用户资料围栏内正文。已有正确 Markdown 和单段短文保持原样，不把“原始答复”等资料标签写入正文。代码、路径、引用和 JSON 的字符及缩进保留；中文弯引号给出 `\u201c` 等 JSON 转义示例，解码后仍是原字符。引号不规范化为英文引号，不事后替换或修复模型原始 JSON。

取消接口约束会增加模型直接生成非法 JSON 的可能性；实验中确实观察到引号未转义而被本地校验拒绝，见[失败样本](../../../../experiment/assistant-rendering-prompts/20261030-154740-layout_json_v2-code_literals/analysis.md)。改进序列化示例后，该样本的代码、路径、中文引号和字面反斜杠已通过[复测](../../../../experiment/assistant-rendering-prompts/20261030-155029-layout_json_v3-code_literals/analysis.md)。保真仍依赖模型，本地结构校验不保证语义正确；无效输出依旧终止，不增加自动重试、不发送半成品。

本地 `.env` 与配置模板已恢复 `ASSISTANT_RENDERING_THINKING_EFFORT=medium`，部署说明同步。实验的 1800 秒仅覆盖实验请求；应用超时仍沿用 `VLLM_MLLM_TIMEOUT_SECONDS`。本次没有主动重启服务，运行进程须重新加载代码和配置后才采用新方案。逐次原始产物、失败记录和最终样本验证汇总见[实验索引](../../../../experiment/assistant-rendering-prompts/README.md)。

最终验证：6 个真实非流式样本全部通过，覆盖长表格、条件与缺失值表格、正确 Markdown、代码/路径/引号、短回复及未出现在提示示例中的短句；实际消息与应用提示逐字一致。长文保留 49 个换行、17 个列表项。42 项隔离测试、两套 Compose 配置和两个仓库的差异格式检查通过；没有新增失效文档链接。完整证据和限制见[最终分析](../../../../experiment/assistant-rendering-prompts/20261030-final-review/analysis.md)。
