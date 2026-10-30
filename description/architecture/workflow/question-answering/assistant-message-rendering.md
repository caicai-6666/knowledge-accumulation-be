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

采用非流式 JSON Schema 约束生成，[唯一输出字段](../../../../app/agent/assistant/message_rendering/schema.py)为：

```json
{"content":"转换后的完整钉钉 Markdown 正文"}
```

Schema 要求 `content` 是非空字符串，禁止额外字段；本地进一步拒绝纯空白、重复 JSON 键、非标准数值、JSON 围栏、拒绝响应、工具调用和截断响应。原生 `reasoning` / `reasoning_content` 不放入 JSON Schema 或钉钉正文，完整保存在私有生成记录中。

每次 `render` 最多调用一次生成，不进行内部重试、多轮纠错或二次总结。失败没有下一轮模型请求，安全定向原因沿 Core 错误返回宿主，不能声称已通知模型纠错。无效结果、取消或审计失败终止任务，不提交原文、不继续后续钩子，也不自动重放主助理。

---

## 钉钉规则与保真边界

官方依据为[机器人发送消息类型：Markdown 支持的语法](https://open.dingtalk.com/document/orgapp/types-of-messages-sent-by-robots)。其明确针对 `sampleMarkdown` 列出一至六级标题、引用、加粗、斜体、链接、图片、有序及无序列表，并建议真实换行前后分别添加两个空格。

转换提示限定使用上述格式，段落及分组留空行，普通 `•` 条目转为 Markdown 列表，装饰横线改为章节标题；表格按行或对象纵向展开，保留字段对应关系。代码、JSON、公式和逐字引文内部的字符及必要空白属于内容，保留原样，仅可移除外围围栏。不得增加事实、来源、链接、图片、推测或承诺，不得为简短而删减正文。

上述保真和排版规则属于模型语义约束；程序验收结构与生成完整性，不以简单正则误删代码或引用，也不宣称结构校验能证明语义完全不变。实际模型转换质量和不同钉钉客户端展示仍需独立验收。

---

## 生成配置与资源

`DingTalkMessageRenderer(settings, thinking_effort='medium', client=None, counter=None)` 复用[MLLM 客户端](../../../capability/infrastructure/vllm-models.md)，强制 `enable_thinking=true`，独立选择推理档位；其他采样参数、模型地址、输出额度与超时沿用显式传入的 MLLM 配置。档位按已有模型协议映射，不在提示词中添加推理档位或工具格式模板。

应用配置 `ASSISTANT_RENDERING_THINKING_EFFORT` 默认 `medium`，可选 `low / medium / xhigh`，与主助理 `ASSISTANT_THINKING_EFFORT` 独立。使用与生成相同配置调用原生分词，预留最大输出额度；超预算直接失败，不截断原文。分词的基础设施瞬时重试不计为第二次生成。

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

[转换器测试](../../../../tests/test_assistant_message_rendering_agent.py)通过真实 `MLLMClient` 和 HTTP MockTransport 检查两条上下文、medium 原生推理、JSON Schema、一次生成、异常审计与 Core 提交集成。[交付测试](../../../../tests/test_assistant_rendering.py)验证直接发送权威正文及原有抢占和不确定性；[生命周期测试](../../../../tests/test_robot_initialization_lifecycle.py)和[装配回归](../../../../tests/test_registry_service_identity.py)检查默认/自定义工厂钩子注入、档位及逆序关闭。

以上使用内存、网络与应用资源替身，不访问业务数据库或向员工发送消息；真实模型语义保真、真实 SQL 往返及钉钉手机端效果不在本轮验证结论内。

2026-10-30 验证：助理、Core 钩子、原生调用/参数纠错、应用装配及平台 A2A 相关回归共 100 项，93 项通过、7 项因未启用隔离 MySQL 环境跳过。变更源码语法、转换专题链接及 `git diff --check` 检查通过。测试未请求真实模型或钉钉，未修改真实 `.env`、业务数据库或重启服务。
