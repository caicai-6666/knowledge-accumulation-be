# vLLM 模型配置与异步客户端

## 用途与当前状态

参考 contract-service-be 的通用模型参数、异步 Chat Completions、工具流式响应和原生分词协议，提供本项目的 MLLM 基础设施；Embedding 继续使用已适配的 LlamaIndex 客户端。MLLM 按需创建，已供内部问答 Core 调用；配置 Embedding 地址后，FastAPI 启动时装配[记忆自动向量化](../application/memory-embedding.md)，后台扫描并请求向量模型。

配置分离为 [MLLM](../../../app/config/mllm.py)和 [Embedding](../../../app/config/embedding.py)，工具型 Agent 支持[可选工具格式模板](tool-tag.md)。本项目不沿用合同提取、业务门禁、视觉页面渲染或推理窗口的专属配置。完整原生推理由客户端返回，不按窗口丢弃；[节点落盘层](../../architecture/data/node-persistence.md)已提供显式保存接口，内部问答 Core 已接入，外部探索问答路由已装配。

---

## 填写配置

[.env.example](../../../.env.example)与本项目实际 `.env` 均已准备配置区；本次实际文件仅补充缺失的 27 项 `VLLM_*` 配置，所有此前值原样保留，未复制其他项目真实地址或密钥。

首先填写两个地址，支持服务根地址或含代理前缀的 `/v1` 基础地址，不填写完整 `/chat/completions` 或 `/embeddings` 路径：

```dotenv
VLLM_MLLM_BASE_URL=http://你的主模型服务地址/v1
VLLM_EMBEDDING_BASE_URL=http://你的向量模型服务地址/v1
```

模型默认分别为 `qwen38-flash-next`、`qwen3-vl-embedding-8b`，沿用参考项目公开示例命名；服务 `served-model-name` 若不同，必须修改对应 `VLLM_*_MODEL`，不能仅凭地址推断模型。服务需要认证时填写对应 `API_KEY`，无认证则留空。Embedding 默认 4096 维，必须与服务真实输出和 MyVector 索引声明一致，更换模型需统一重建历史向量。

加载顺序为本项目 `.env`，再由进程环境覆盖；不展开 `${...}`。地址为空不影响现有应用启动；Embedding 地址已配置时会在启动期间校验其完整配置，MLLM 仍在按需加载时校验，错误只返回脱敏提示。数值和布尔示例不加引号，API key 以 `SecretStr` 保存。

---

## MLLM 配置项

| 配置前缀 `VLLM_MLLM_` 后缀 | 默认值与用途 |
| --- | --- |
| `PROVIDER` / `ENDPOINT` | `vllm` / `chat_completions`，目前不支持其他协议。 |
| `BASE_URL` | 留空待填写，不允许 URL 内嵌凭据、查询参数或片段。 |
| `MODEL` / `API_KEY` | 参考模型名 / 空密钥。 |
| `TOOL_TAG_ENABLED` / `TOOL_TAG_FILE` | true / qwen3.8-flash-next-nvfp4.txt；[格式模板、路径与强度映射](tool-tag.md)。 |
| `TIMEOUT_SECONDS` | 300，包含主模型配额等待和完整流式消费的总时限。 |
| `MAX_CONCURRENT_REQUESTS` | 20，同事件循环内全部 MLLM 客户端及分词请求共享。 |
| `CONTEXT_WINDOW_TOKENS` | 262144，最大生成长度必须小于该值。 |
| `ENABLE_THINKING` / `REASONING_EFFORT` | true / xhigh，强度可选 low、medium、xhigh。 |
| `TEMPERATURE` / `TOP_P` / `TOP_K` | 0.7 / 0.8 / 20。 |
| `MIN_P` | 默认未设置，不发送；可显式设置0至1的有限数值。摘要独立固定为0。 |
| `PRESENCE_PENALTY` / `REPETITION_PENALTY` | 1.5 / 1.0。 |
| `SEED` / `MAX_COMPLETION_TOKENS` | 3407 / 8192。 |

生成参数集中于不可变 `MLLMGenerationSettings`。如单次请求需要覆盖参数，应构造经校验的新配置并传入 `generation`，分词时也必须传入相同配置。上述档位沿用参考项目的通用语义；Qwen、GLM 与 DeepSeek 按显式工具模板文件名进行[统一强度映射](tool-tag.md#分词与生成一致性)，不根据任意模型别名猜测协议。

主题发现和总结固定读取配置的 `REASONING_EFFORT`、输出额度及seed，不继承用户本轮档位；其采样参数独立于主任务，详见[摘要独立生成配置](../../architecture/workflow/question-answering/topic-summary.md#摘要独立生成配置)。

Embedding 的全部配置、批量与归一化行为见 [MyVector 配置说明](myvector.md#配置与生命周期)，不在此复制第二份定义。

---

## MLLM 客户端

[MLLMClient](../../../app/infrastructure/mllm.py)使用 `AsyncOpenAI`，支持上下文管理和幂等 `close()`：

```python
from app.config.mllm import load_mllm_settings
from app.infrastructure.mllm import MLLMClient

settings = load_mllm_settings()
async with MLLMClient(settings) as client:
    result = await client.create_chat_completion(
        messages=[{"role": "user", "content": "请说明企业知识沉淀的作用。"}],
    )
    answer = result.content
```

示例仅供显式调用，导入不会发起请求。`messages` 可携带 OpenAI 兼容多模态内容，客户端不负责 PDF 渲染、图像存储或媒体 UUID 缓存。默认生成请求附带配置的采样参数和 `chat_template_kwargs`，不自动加入系统提示词、工具协议或业务上下文。

| 方法 | 行为 |
| --- | --- |
| `create_chat_completion(messages=..., json_schema=...)` | JSON Schema 约束正文输出；不能同时启用工具，业务层仍做完整校验，供主题总结使用。 |
| `create_chat_completion(messages=..., generation=...)` | 普通非流式请求，返回完整助手消息、用量、结束原因及原始响应。 |
| `create_tool_chat_completion(messages=..., tools=..., tool_choice=...)` | 保留原生工具调用及参数字符串，不解析或执行业务工具。 |
| 上述方法的 `stream=True` / `on_delta=callback` | 消费流并合并正文、推理和工具参数，完成后返回统一结果；回调非空时自动启用流式。 |
| `close()` / `async with` | 关闭 SDK 与底层 HTTP 客户端，关闭后请求在本地失败。 |

函数工具请求设置 `parallel_tool_calls=False`；基础客户端仍完整返回实际工具列表，单工具业务约束留给后续 Agent 执行器。工具 Schema 的字段说明由定义方保证，客户端不补造参数定义。

原生 `reasoning`、`reasoning_content` 原样保留，流式碎片按收到的顺序拼接，不设置 K 轮窗口、不截断正文。`raw_response` 保存完整响应，流式另有 `raw_chunks`。这些是内部结果，不应直接传给前端或拼进系统提示词；是否再次注入模型由上下文策略决定。流式失败时不返回伪造的成功结果，已接收片段可由内部回调记录；调用方可通过节点落盘层保存这些片段，客户端不自动写库，不宣称所有失败生成已经持久化。

只允许一份生成候选。重复/非法工具结构、缺失结束原因、无效响应、流式结束后继续返回内容等情况拒绝；`finish_reason='length'` 等状态仍原样交给上层判断，不视为业务完成。原始参数字符串中的 JSON 与业务校验不在客户端执行。

---

## 分词与共享并发

[vllm_tokenizer](../../../app/infrastructure/vllm_tokenizer.py)提供 `count_text_tokens` 和 `count_chat_tokens`，调用模型服务原生 `/tokenize`，不下载 tokenizer、不回退字符估算。`/proxy/v1` 对应 `/proxy/tokenize`，保留代理前缀。

纯文本计数关闭额外特殊 token；聊天计数包含实际 messages、tools 和生成前缀，使用与生成相同的思考开关、强度和调用方模板参数。返回值必须是非负整数，并与 token ID 列表长度一致；布尔、数字字符串、缺失列表和非法 ID 均拒绝。

[model_concurrency](../../../app/infrastructure/model_concurrency.py)沿用参考项目的事件循环级共享配额：MLLM 与 Embedding 分开，同类模型不同客户端共享额度。关闭一个客户端不会重置配额；同一事件循环内不允许初始化不同最大值，改变配置需重启。Embedding 单条、批量和查询已统一使用该配额。

---

## 错误与请求边界

所有 HTTP 不信任环境代理，不跟随重定向；密钥、地址、用户正文及服务端原始错误不进入自定义错误消息。生产环境的调用层同样不应打印原始响应或请求体。

- MLLM 生成不自动重试，避免重放流式生成：404 为 `MLLMProviderNotReadyError`；连接、超时、限流及临时服务错误为 `MLLMUnavailableError`；永久拒绝或协议异常为 `MLLMRequestError`。
- 分词是只读请求，仅对连接及选定临时状态最多尝试 3 次，失败抛出 `TokenizationError`；不因此重放任何模型生成或工具动作。
- Embedding 保留既有 LlamaIndex/SDK 请求重试配置；自动加工失败记录由后台在后续扫描轮次重试，不重放问答生成。
- 主模型取消直接传播并关闭流，不能因收到部分正文就提交任务完成。分词和 Embedding 也不吞掉取消；上层问答终态由运行时持久化；后台向量化取消保持记忆可恢复，不把取消记作模型失败。

---

## 验证

测试文件：[配置加载](../../../tests/test_model_config.py)、[MLLM 协议](../../../tests/test_mllm.py)、[分词](../../../tests/test_vllm_tokenizer.py)、[Embedding](../../../tests/test_embedding.py)。配置测试显式写入并清理 `tests/tmp/`；HTTP 使用 MockTransport，不请求填写的实际地址。

覆盖只填两地址的默认模型配置、环境优先级、密钥脱敏、预算及非有限参数、普通/工具/流式响应、长推理保留、断流、HTTP 错误、取消关闭、共享并发、分词计数及生成/分词参数一致性。此处离线用例不代表真实联通；真实服务样例另见后续验证记录。

2026-10-21 验证：模型配置/MLLM/分词/Embedding 定向 28 项通过；使用独立临时 MyVector 容器运行全量回归，262 项通过，无跳过，其中 24 项 MySQL/MyVector 实库测试。`pip check` 无冲突，新改文档 50 个相对文件链接通过。配置核对确认两类模型共 27 个键齐全、两个地址均留空、原有实际配置保留；未调用在线模型或共享服务，临时容器及匿名卷验证后清理。

2026-10-21 真实服务验证：用户填写地址后，普通回答、流式工具与原生推理、文本/聊天分词、4096 维 Embedding 及图片输入均完成小样本验证。输入和结果见[实验记录](../../../experiment/vllm-live/20261021-check/analysis.md)，不等同完整问答工作流验收。此前 262 项为历史回归，本次完整回归 280 项通过。

内部 Agent Core 使用 `on_raw_chunk` 审计回调在解析前接收原始流块，以便断流、取消和协议错误时保存已收到的片段；该回调不直接发布给前端。运行循环及节点装配见[核心循环](../../architecture/workflow/question-answering/agent-core.md)。
