# 节点记忆的 MyVector 检索

## 用途与实现来源

沿用同级 registry-be 已验证的 MyVector 四参数 UDF、HNSW/Cosine、候选扩展和精确兜底实现，适配本项目 asyncmy、节点记忆和用户归属。Embedding 沿用其 LlamaIndex `OpenAILikeEmbedding` 对接 vLLM 的异步方式，不使用全局 `Settings.embed_model`。

本模块已提供可独立调用的基础设施，未挂载问答路由、模型工具、启动任务或自动向量化消费者。现有应用启动不加载 Embedding 配置，不自动建表、清空索引或向模型发请求。真实 `.env`、共享数据库及其他项目均不改动。

---

## 模块与数据分工

| 模块 | 职责 |
| --- | --- |
| [Embedding 配置](../../../app/config/embedding.py) | vLLM 地址、模型、预期维度、并发与超时，错误信息不回显密钥。 |
| [Embedding 服务](../../../app/service/embedding.py) | 异步单条、批量及查询编码，校验非零有限向量及维度，默认 L2 归一化。 |
| [MyVector 适配](../../../app/infrastructure/myvector.py) | LlamaIndex 向量库协议、独立缓存刷新、四参数 ANN、索引重建及精确查询。 |
| [记忆检索服务](../../../app/service/question_memory.py) | 读取权威记忆、生成范围过滤、刷新缓存、搜索并回查权威记录。 |
| [节点仓库](../../../app/repository/question_tree.py) | `load_searchable_memory_documents` 读取本人指定树内成功记忆和原文，可限定候选 ID 回查。 |

业务表 [question_node_memories](../../architecture/data/question-memory.md)仍保存原始 float32 向量，不新增原文、哈希、模型或维度字段。[003-question-memory-index.sql](../../../schema/003-question-memory-index.sql)新增独立 `question_memory_index` 缓存，必须在前两份迁移之后手工执行。

缓存字段为自增 `id`、唯一 `memory_id`、`owner_userid`、`tree_id`、`node_id`、`kind`、插件编码的 `embedding` 和原文副本 `text`。不依赖 registry 的 `myvector_documents`，也不以缓存代替权威消息及记忆。原始 float32 BLOB 不能直接当作插件格式，写入缓存时使用 `myvector_construct` 重新编码；返回的 LlamaIndex TextNode 携带原文副本及记忆引用，返回前仍需权威回查。

索引列声明初始为 `HNSW / dim=4096 / size=10000 / Cosine / M=16 / ef=100 / idcol=id`。这是一份部署配置，不代表任意运行时维度都可自动写入；修改维度、容量必须显式迁移列声明，不能由应用在查询时偷偷执行 ALTER。

---

## 查询流程与权限

`QuestionMemorySearch.search(tree_id, owner_userid, vector, top_k=5, min_similarity=-1, kind=None, node_id=None)` 是内部异步入口：

1. 校验向量、Top-K、阈值及过滤类型。
2. 通过仓库校验树归属，读取该树全部成功记忆及公开原文；数据库失败直接上抛，绝不降级为空列表删除缓存。
3. 校验全部向量后，在适配器同一实例锁内刷新当前用户/树缓存、重建并查询；空权威集合只清理该范围缓存并返回空。
4. 用户和树必须使用 AND 相等过滤，可以再按记忆类型或节点 ID 过滤。禁止无归属、OR、嵌套、未知或重复字段过滤。
5. 通过四参数 `myvector_ann_set` 获取候选，在同一条 SQL 中使用 `myvector_row_distance(id)` 读取对应距离，返回 `1 - distance` 相似度。
6. 按候选 ID 再次读取权威成功记忆及原文，排除查询期间删除、状态变化或向量变化的记录；核对索引中的用户、树、节点和类型，最后返回 `memory_id/node_id/source_id/kind/text/similarity`。

服务要求调用方传入已认证的用户标识，本身不验证用户 token。查询范围目前是明确指定的一棵树，不会自动跨树或跨用户搜索；是否只检索祖先路径由未来模型工具契约决定，不能把记忆召回与上下文父链回溯混为一谈。返回引用不表示已回查原文；规范化消息来源由节点落盘层提供，需要调用方另行读取。

默认至少获取 100 个或 `10 × top_k` 的 ANN 候选，再扩大候选，最多 10000 个。到达候选上限仍不足时使用 `myvector_distance(...,'Cosine')` 的精确 SQL 兜底；全表已进入候选时返回实际匹配数量。Top-K 为 1 至 100。HNSW 是近似检索，候选扩展不保证任意数据分布下都与精确排序相同；`search_mode='exact'` 可作为精确基线。

---

## 配置与生命周期

示例维护在 [.env.example](../../../.env.example)，不复制其他项目真实地址或密钥。

| 配置 | 用途 |
| --- | --- |
| `VLLM_EMBEDDING_PROVIDER` / `ENDPOINT` | 固定 `vllm` / `embeddings`。 |
| `VLLM_EMBEDDING_BASE_URL` | OpenAI 兼容服务地址，允许根地址或 `/v1`，不允许地址内含凭据或查询参数。 |
| `VLLM_EMBEDDING_MODEL` | 实际模型名称，允许自定义名称。 |
| `VLLM_EMBEDDING_DIMENSIONS` | 预期维度，1 至 4096，示例 4096；必须匹配索引列声明。 |
| `VLLM_EMBEDDING_API_KEY` | 可选密钥，不进入日志和检索结果。 |
| `VLLM_EMBEDDING_TIMEOUT_SECONDS` | 默认 60。 |
| `VLLM_EMBEDDING_BATCH_SIZE` | 默认 32。 |
| `VLLM_EMBEDDING_MAX_CONCURRENT_REQUESTS` | 默认 10，单条、批量和查询共用限制。 |
| `VLLM_EMBEDDING_NORMALIZE` | 默认 true，客户端 L2 归一化。 |

`EmbeddingService(settings)` 由调用方创建并关闭，文本通过 `embed_text` / `embed_texts` 生成向量，查询可使用 `service.model.aget_query_embedding`。HTTP 不信任环境代理、不跟随重定向，SDK 重试最多 2 次；模型与维度来自同一配置，文档请求不传维度裁剪参数。并发额度已统一到事件循环级，多个 EmbeddingService 实例共享，见 [vLLM 配置与生命周期](vllm-models.md#分词与共享并发)。

创建 `MyVectorStore(cache_pool, dimensions=settings.dimensions)` 时，必须提供独立的 `asyncmy` **autocommit=True** 连接池，并由调用方关闭；不能直接传入节点仓库的 autocommit=False 事务池。再通过 `QuestionMemorySearch(repository, store)` 注入。缓存自身的替换操作使用显式事务，索引管理在提交后执行。

插件必须预先安装，并配置其内部 `myvector.cnf` 连接、可写索引目录和索引管理存储过程权限；普通 MySQL 镜像不能替代带插件镜像。只具备 `myvector_distance` 函数不代表 HNSW 索引目录和内部连接已经可用。插件不可用、权限不足或索引配置错误直接失败，不假装无记忆，也不自动安装插件。

本轮不保存逐记录模型标识，因此全部记忆必须来自统一模型；仅维度一致不能识别模型混用。更换模型或同名模型权重时须统一重建业务向量和索引。此切换必须由部署和数据迁移流程保证，当前代码不会自动识别历史模型。

---

## 一致性与规模限制

缓存刷新、索引重建和检索由同一适配器实例锁串行执行。每次业务检索先刷新对应树的权威快照；增删会标记索引 dirty，查询前重建并校验类型、维度、容量及距离配置。索引可从业务向量重建，不将插件缓存作为业务持久化的唯一副本。

本轮沿用已验证的“更新后重建”策略，不能宣称大规模增量维护、跨进程并发、外部索引写入和大库重启加载已经优化。每次检索读取整树成功向量并刷新，会带来线性读取和重建成本。实例锁不协调其他实例；同一缓存表当前只支持一个维护实例，索引表不可由其他服务并发维护。

仓库、缓存或插件异常向上传播，取消和超时不自动重放；插件副作用发生后回执丢失仍可能存在不确定性，下次查询重新刷新并构建。索引容量不足显式失败。缓存失败不修改原节点、原文、工作区及成功记忆向量。

---

## 隔离验证

离线测试：[Embedding](../../../tests/test_embedding.py)、[过滤与四参数协议](../../../tests/test_myvector.py)、[权威记忆回查](../../../tests/test_memory_search.py)。

实库测试：[MyVector 集成](../../../tests/test_myvector_integration.py)使用与 registry 验证相同的固定镜像：

```text
ghcr.io/askdba/myvector:mysql8.4@sha256:219c74a5735c3150391cd4b818d148baddabf229ce6cb0eaa05f5f2070d3ae7f
```

仅在独立一次性容器、固定测试库 `knowledge_question_tree_test` 及随机宿主端口运行。测试会创建七张业务表和一张索引缓存表、配置容器内部索引连接和目录；不挂载默认 `data/` 或业务目录，不读取真实 `.env`。使用 `QUESTION_MEMORY_TEST_MYVECTOR=1` 与 `QUESTION_TREE_TEST_MYSQL_PORT` 两个显式测试参数，普通回归默认跳过插件测试。

```bash
QUESTION_MEMORY_TEST_MYVECTOR=1 QUESTION_TREE_TEST_MYSQL_PORT=独立临时端口 python -m unittest tests.test_myvector_integration -v
```

测试覆盖真实插件排序、类型及用户/树过滤、250 个范围外近邻干扰、候选扩展与精确兜底、删除后刷新、索引维度错误，以及 LlamaIndex 异步插入/检索。Embedding HTTP 使用 MockTransport，不声称已连接真实 vLLM。小规模验证不代表生产数据规模下的性能或召回率。

2026-10-21 验证结果：本项目 Conda 环境已安装两项 LlamaIndex 直接依赖，`pip check` 无冲突；携带独立容器端口和插件测试标志运行全量回归，239 项通过，无跳过，其中 24 项为 MySQL/MyVector 实库测试。新改文档 64 个相对文件链接通过。临时容器及匿名卷在验证后清理；其他项目、真实配置、共享业务数据及索引未修改。

节点原文可通过[节点落盘层](../../architecture/data/node-persistence.md)的 `load_memory_source` 按归属回查；检索本身仍返回引用，尚未自动拼接模型上下文。

全文缓存迁移、独立刷新及只读关键词查询见[节点记忆全文检索](fulltext.md)。旧缓存先执行 005 迁移，再显式刷新正文；向量列仍不可为 NULL。
