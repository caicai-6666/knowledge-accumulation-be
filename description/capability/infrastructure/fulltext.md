# 节点记忆全文检索

> **当前部署入口：** 新空库仅执行 deploy 的[最新初始化脚本](../../../../knowledge-accumulation-deploy/mysql/init/001-schema.sql)。本文中的编号迁移和旧库操作属于历史演进记录，不是新部署步骤；历史 SQL 已从当前仓库移除，仅可从 Git 历史追溯。已有数据库须另行制定升级方案，不可重跑初始化。

## 用途与边界

在已有 `question_memory_index` 检索缓存中保存原文副本，为用户提问、完整阶段输出和最终答案提供关键词检索。使用 MySQL 8.4 内置 `FULLTEXT`、`ngram` 和 `MATCH ... AGAINST`；无需额外安装全文插件或更换 asyncmy。原生推理不进入全文缓存。

[消息表](../../architecture/data/node-persistence.md)是权威原文，[记忆表](../../architecture/data/question-memory.md)是权威向量；缓存可以删除重建。本文维护全文查询、刷新及部署契约，向量算法见 [MyVector 说明](myvector.md)。未新增 HTTP 路由或后台缓存刷新任务；[祖先任务记忆工具](../../architecture/workflow/question-answering/memory-retrieval.md)已接入按需缓存刷新、范围内关键词召回及 RRF 混合排名，权威向量由[后台加工](../application/memory-embedding.md)生成。

> **当前范围：** 共用缓存仍要求非空有效向量，因此全文只覆盖已向量化成功的记忆。pending/failed 记忆尚不可全文查询。固定 MyVector 镜像在含 NULL 向量的索引构建验证中进程异常退出，本轮保留既有非空约束，不填充假向量，也不宣称已支持纯文本先行索引。

---

## 数据与迁移

[005-question-memory-fulltext.sql](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/005-question-memory-fulltext.sql)在 `001` 至 `004` 后手工执行，为检索缓存增加：

| 字段或索引 | 用途 |
| --- | --- |
| `text LONGTEXT NOT NULL` | 可重建的公开消息原文副本。 |
| `FULLTEXT ft_question_memory_text (text) WITH PARSER ngram` | MySQL 内置中文字符片索引。 |

不向 `question_node_memories` 增加 text、hash、model 或 dimensions。原文通过记忆 source_id、node_id、kind 与规范化消息三者匹配获取，不能仅凭相同 ID 跨节点取文。旧底层接口登记的自定义引用若没有规范化消息，正文为空，保留原有向量检索能力，不伪造可全文检索的原文。

已有缓存执行迁移后原文为空，需要显式刷新；应用不会自动执行 DDL 或改真实配置。迁移可能重建大表，部署时安排维护窗口，不能在请求中执行 ALTER。

---

## 内部调用

继续复用 [QuestionMemorySearch](../../../app/service/question_memory.py)、[MyVectorStore](../../../app/infrastructure/myvector.py)及其独立 autocommit=True 连接池，不增加依赖、连接池或后台任务。

```python
# 在消息及向量成功提交后，由独立加工阶段调用，不能塞入节点权威事务。
await search.refresh(tree_id, authenticated_userid)

# 查询不刷新整树、不调用 Embedding、不构建 HNSW。
matches = await search.search_keywords(
    tree_id, authenticated_userid, "新员工如何申请知识库权限？",
    top_k=5, kind="final", node_id=None,
)
```

| 方法 | 行为 |
| --- | --- |
| `refresh(tree_id, owner_userid)` | 读取本人整树成功记忆及消息原文，校验后事务替换该范围缓存，返回条数。 |
| `search_keywords(..., text, top_k=5, kind=None, node_id=None)` | 只读缓存，返回 memory_id/node_id/source_id/kind/text/score。 |
| `MyVectorStore.keyword_query` | 底层全文查询；不能代替服务层所有权校验。 |
| `MyVectorStore.replace_scope` | 显式范围替换；不在此构建向量索引，只标记 dirty。 |

text 必须非空、至多 4096 字符；Top-K 为 1 至 100 的整数。用户与树范围必填，可以增加 question/intermediate/final 或节点过滤。不接收无范围查询，SQL 参数化传入整句；采用自然语言模式，不执行用户输入中的布尔语法或 SQL。

按相关性分数降序，再按 memory_id 稳定排序；只返回正分数。分数不是概率或余弦相似度，也不称为标准 BM25，不与向量分数直接相加。当前没有自动融合两路结果。

---

## 中文、英文与短词

采用服务器默认 `ngram_token_size=2`：例如“知识库权限”切分为“知识、识库、库权、权限”。这是字符片切分，不理解中文词义。自然语言模式按字符片联合匹配，英文词也被切分，可能因少数字符重叠命中不够相关的内容。

单字、停用词及无有效字符片的查询可能没有结果；查询不回退 LIKE，也不自动改数据库参数。MySQL 默认停用词表包含英文词，ngram 对包含停用词的字符片也会排除，需结合业务样本评估。此行为与语义向量召回互补，不能期待自动理解同义词或错别字。

部署核对 utf8mb4、全文索引 parser、`ngram_token_size` 和 InnoDB 停用词配置。修改切分长度需重启数据库并重建全文索引；修改停用词配置也需相应重建。分数统计基于整个全文索引语料，SQL 用户/树过滤不等于每个用户独立的 IDF 统计。

---

## 一致性、性能与失败

刷新与查询复用适配器唯一实例锁。数据库读取、来源解析或向量校验失败时，不使用空列表清缓存；事务写入失败回滚，原文和向量同步更新。向量接口也写入并返回 text，不会把已有全文缓存擦成空正文。

关键词查询先验证树归属，只在数据库里对指定范围做全文匹配；不为了检索而联表扫描权威正文。命中后仅回查最多 Top-K 条权威记录，核对归属、类型、来源存在性及原文一致性；删除、失效或原文变化的候选不返回，可能少于 Top-K，不自动无限补查。依赖异常向上抛出，不伪装无命中。

独立缓存避免权威节点提交同步维护全文索引，但共享 MySQL 实例仍有 CPU、内存和 I/O 竞争。`refresh` 当前读取整树并替换缓存，仍有线性成本；关键词查询本身不会触发它。原向量 `search` 为保留既有一致性行为，仍先刷新再检索。增量刷新、队列消费者、多实例写协调及性能基准尚未实现；同一缓存表仍限定单维护实例。

---

## 验证

[离线测试](../../../tests/test_fulltext.py)验证查询形状、只读路径、参数化、过滤、权威回查、陈旧文本和刷新故障。[隔离 MySQL 测试](../../../tests/test_fulltext_mysql.py)验证真实 ngram 中文/英文查询、正分排序、短词/无命中、过滤、原文返回、删除、旧缓存迁移及向量兼容。统一环境与固定镜像见 [MyVector 隔离验证](myvector.md#隔离验证)。所有输入为合成消息，不读实际 `.env`、真实数据库或 data 目录。

2026-10-21 验证：完整 295 项通过，无跳过，其中 45 项独立临时 MySQL/MyVector 测试；新增全文相关 8 项离线和 7 项实库验证。`pip check` 无冲突，本次修改文档 66 个相对文件链接有效。未修改真实数据库、实际配置或其他项目，临时容器及匿名卷验证后清理。
