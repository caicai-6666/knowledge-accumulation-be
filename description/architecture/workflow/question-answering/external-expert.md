# 外部专家问答工具

两个专家工具独立注册末尾渲染钩子。成功直接展示已有排版的专家答复，保留完整会话标识、轮次和原文，不做二次总结、不再包装为 JSON；失败展示产生分支提供的原因及处理方向。专家服务托管的联网工具协议由服务端处理，本项目没有另一套可迁移的本地专家工具循环；会话和私有审计契约不变。 渲染及兼容边界见[工具反馈契约](tool-content-contract.md#core-工具反馈迁移清单)。

## 用途与实现边界

参考 contract-service-be 的专家工具，将首次求助、连续追问、固定规则、可选职责、答复渲染及 LRU 会话语义迁入公共 Core。适用于专业知识不足、复杂判断或需要第二意见的场景；专家只接收主助手主动提交的文字，不自动看到用户资料、附件、工作区或树历史。

实现入口为 [external_expert.py](../../../../app/agent/core/tool/external_expert.py)，通过[统一工具注册契约](tool-content-contract.md)运行。探索和助理的 `build_service` 均在节点运行时绑定可信用户及树，底层 `build_core` 的无资源基础工具集合不变；独立 Core 调用方可显式添加 `build_external_expert_registrations(get_pool=..., on_audit=...)`。

源项目的参数说明、固定共同规则、默认职责、职责编写指导和答复包络保持一致；适配点是本项目的 `RegisteredTool`、`ToolExecutionResult`、树身份与私有节点审计。没有迁入合同门禁、独立网页工具或参考项目的 FIFO 实现。专家正文先交给主助手，用户只接收主助手正式提交的中途和最终输出；网页工具另有独立动作提示，见[网页检索专题](web-search.md#动作提示与-sse)。

---

## 工具参数与专家职责

| 工具 | 参数 | 规则 |
| --- | --- | --- |
| `ask_external_expert` | `question` | 必填本轮完整专业问题及必要背景，专家不自动读取附件 ID 或“见上文”。 |
| `ask_external_expert` | `system_prompt` | 可选职责；省略/null 使用默认职责，空白拒绝；首次创建后不可修改。 |
| `continue_external_expert` | `session_id` | 同一用户树中先前成功求助返回的完整专家会话标识。 |
| `continue_external_expert` | `question` | 本轮追问、补充或更正；程序回传已成功的原生历史。 |

两个模型均拒绝未知字段、非文字值和空白正文。首次工具不接受会话标识或额外 `context`；追问不接受职责变更或 `context`。所有字段直接在工具 JSON Schema 上提供完整中文说明。

专家 system 消息为“固定共同规则＋默认/自定义职责”，不能通过参数删除共同规则。问题以 `## 求助问题` 独立 user 消息发送。共同规则要求事实与判断分离、使用真实可核验来源、保留未知和适用条件；自定义职责不得要求伪造来源或预设结论。提示词的业务规则不是已经实现的任意文本语义验证。

模型可见内容不含用户 ID、树/节点 ID、运行标识或提示词版本。`session_id` 是追问所需的业务句柄，只代表本地专家会话，不是用户 token 或远端持久会话。

---

## 答复与上下文

成功结果包含 `status=success`、`session_id`、`round` 和 `content`。`content` 使用源项目包络，专家正文原样保留：

```text
════════════ 外部专家答复 ════════════
专家会话：〈完整专家会话标识〉
对话轮次：2

以下内容为外部专家意见，需结合其依据、适用条件和不确定性判断。

〈专家答复正文〉

────────────────────────────────
如需追问或补充背景，请调用 continue_external_expert，
并使用上方完整的专家会话标识。
════════════ 专家答复结束 ════════════
```

两个工具执行期间通过 `task.progress` 发布 `type=external-expert`，提示“正在咨询外部专家：<问题>”；展示文字压平空白并截断到 2000 字符，发给专家的问题不变。执行结束、失败或取消后注册表恢复 `thinking`，任务终态后 SSE 不再发布动作提示。该状态独立于任务的推进/探索模式，不进入模型上下文或公开消息正文；作为展示事件保存并可在打开树时恢复，不能作为咨询成功的依据。事件契约见[订阅并激活探索任务](../../../api/exploration.md#订阅并激活探索任务)。

结果类型为 `ordinary`，不按页面折叠或使用图片存活轮次。模型收到统一工具回执中的结构化结果，`content` 字段内即上述文本。专家自己的后续请求回传原始 Responses output（reasoning、web_search_call、message），不回传这层答复包络；搜索失败状态和适用限制原样保留，不将搜索工具可用当成已经核实。

仅完整有效且未超预算的响应提交一整轮。网络失败、取消、截断和超限不提交半轮；已有会话保留原历史，新建失败撤销临时会话。失败工具回执仅参与连续纠错，下一次成功动作后从主助手上下文清除；私有审计不随之删除。错误包括 `expert_unavailable`、`expert_request_failed`、`incomplete_response`、`session_expired`、`session_busy`、`pool_busy`、`session_limit` 和 `pool_closed`。

---

## 会话隔离、容量与释放

[ExpertSessions](../../../../app/agent/core/expert.py) 由 `QuestionAgentService` 持有，按 `(userid, tree_id)` 取池，同树后续节点可继续追问。标识不能跨用户或跨树使用；从历史分支追问同一专家仍沿用该专家会话最新成功历史，不按所选节点回滚专家会话。需要独立背景或职责时新建专家会话。

每池默认驻留 10 个会话，LRU 驱逐空闲项；全部在途则失败。同一会话不能同时追问，不同会话可并发。每会话最多 20 个成功轮次、100000 个 JSON 序列化字符（包含 system 和原生输出），达到上限明确要求缩小问题或新建，不自动截断/摘要。这是本地保护，不是精确 token 预算。

宿主默认最多驻留 32 棵用户树的专家池，LRU 只驱逐没有在途专家请求的池。删除树先等待生产者结束，再释放对应池；停机取消并等待生产者后关闭所有池和自建客户端。直接注入的客户端由调用方关闭。池关闭后迟到答复不再提交，服务重启/驱逐后旧标识明确过期，不静默续建。

专家会话历史仅驻留内存；完整请求、原生响应、推理、搜索状态和提交结果另保存到当前节点的私有 `question_node_generations.payload`，`purpose=external_expert`。`raw_response` 保存 Responses 原文，`raw_chunks` 保存本次审计事件（此接口是非流式，并非网络分片）；`assistant_message` 保存最后一个原生 message。失败与取消也保存已有产物，网络未收到的内容不能补造。审计存储失败直接停止，不能发布未确认落盘的成功回执；无 SQL 表结构变更。公开树/SSE 不返回这些原生审计。

---

## 配置与连接

[DeepSeekSettings](../../../../app/config/deepseek.py) 从 `.env` 和进程环境读取，进程环境优先；参考值只写入 [.env.example](../../../../.env.example)，本次不修改实际配置或复制凭据。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 支持 Responses 及内置搜索的服务根地址。 |
| `DEEPSEEK_API_KEY` | 空 | 首次创建专家客户端时必填；SecretStr 遮蔽常规展示。 |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 沿用参考项目默认别名，实际支持能力以部署端为准。 |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `none/low/high/max`，独立于主助手本轮强度。 |
| `DEEPSEEK_TIMEOUT_SECONDS` | 300 | SDK 网络请求超时，排队不计入此预算；宿主还有整体任务时限。 |
| `DEEPSEEK_MAX_CONCURRENT_REQUESTS` | 3 | 同一事件循环所有专家客户端共用额度，与 MLLM/Embedding 分开。 |
| `DEEPSEEK_MAX_COMPLETION_TOKENS` | 8192 | 专家输出上限，截断不得视为完整答复。 |
| `AGENT_EXPERT_SESSION_CACHE_MAX_SESSIONS` | 10 | 每棵用户树的专家会话 LRU 容量，正整数。 |

[DeepSeekClient](../../../../app/infrastructure/deepseek.py) 复用现有 OpenAI SDK，每轮调用 `/responses`，显式发送完整 `input`、`reasoning`、输出预算及 `tools=[{"type":"web_search"}]`。不使用 previous_response_id，不移植独立 Search 配置或其他厂商 SDK。默认 HTTP 禁用环境代理、重定向和 SDK 自动重试；注入的 HTTP 客户端由调用方显式控制传输配置，关闭责任转移给 DeepSeekClient。

未填 Key 或配置非法只令首次专家求助失败，不影响应用启动和其他工具。408/409/429/5xx、连接与超时作为依赖不可用，其他 HTTP 错误作为请求失败；反馈不包含 Key、原始错误正文或私有地址。配置改变需重启刷新已创建的客户端及额度。

---

## 隔离验证

测试覆盖实际 Schema、默认/自定义职责、原文渲染、Responses 原生历史回传、失败清理、容量与在途保护、取消、跨用户树隔离、缺配置、本地 HTTP 策略、统一注册表与 Core 连续纠错。临时 MySQL 验证探索任务跨节点追问、原生记录落盘、公开输出隔离、取消、停机与删除释放；所有 HTTP 采用 MockTransport，不调用真实 DeepSeek 或共享服务。

执行方式与完整结果保存在 [本次隔离验证目录](../../../../experiment/external-expert/20261022-isolated/)。本次隔离结果不代表真实模型或联网搜索已联通。
