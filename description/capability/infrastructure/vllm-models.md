# vLLM 模型接入配置

## 用途与范围

本项目通过 `agent-core` 内置的 `VLLMClient`、`VLLMEmbedding` 与 `vllm_counter` 访问部署在 vLLM（OpenAI 兼容接口）上的多模态对话模型与向量模型，不再在 `app/infrastructure/` 中重复维护 HTTP 模型客户端或分词器。

- MLLM 配置加载：`app/config/mllm.py`
- Embedding 配置加载：`app/config/embedding.py`
- 向量合法性校验：`app/service/embedding.py`（`validate_vector`）
- 消费方：`app/service/exploration_runtime.py`、`app/service/assistant_runtime.py`、`app/agent/assistant/message_rendering/agent.py`、`app/service/file_retrieval.py`

---

## 配置项

### 对话模型（`VLLM_MLLM_*`）

| 环境变量 | 说明 |
| --- | --- |
| `VLLM_MLLM_BASE_URL` | vLLM 服务基础地址。 |
| `VLLM_MLLM_API_KEY` | 接口访问密钥（可为空或占位密钥，敏感值不入库、不进日志）。 |
| `VLLM_MLLM_MODEL` | 对话模型名称。 |
| `VLLM_MLLM_CONTEXT_WINDOW_TOKENS` | 模型上下文总窗口上限，用于预算与压缩触发计算。 |
| `VLLM_MLLM_MAX_COMPLETION_TOKENS` | 单次生成最大输出 token 数。 |

`agent-core` 的 `RuntimeSettings` 由 `VLLM_MLLM_CONTEXT_WINDOW_TOKENS` 与 `VLLM_MLLM_*` 生成参数生成，探索模式与个人助理模式共用。探索模式支持按请求传入 `thinking_effort`（`none` / `low` / `medium` / `high` / `xhigh`），由 `agent-core` 统一透传给 `VLLMClient`；个人助理钉钉正文转换器（`DingTalkMessageRenderer`）独立构造 `VLLMClient` 生成参数，不影响主循环配置。

### 向量模型（`EMBEDDING_*`）

| 环境变量 | 说明 |
| --- | --- |
| `EMBEDDING_BASE_URL` | 向量服务基础地址。 |
| `EMBEDDING_API_KEY` | 向量接口密钥。 |
| `EMBEDDING_MODEL` | 向量模型名称。 |
| `EMBEDDING_DIMENSIONS` | 期望输出维度；写入与查询前经 `validate_vector` 严格校验维度与有限值。 |

`VLLMEmbedding` 同时服务于 `agent-core` 内置记忆检索向量化与注册中心文件摘要混合检索（`FileRetrievalService`）。

---

## 验证状态

- 单元测试：探索与助理端到端测试通过 Fake `ChatProvider` / `EmbeddingProvider` 验证装配与预算调用。
- 真实联调：独立测试容器已对接真实 vLLM 服务完成多轮探索问答与流式输出验证。
