# 节点记忆向量存储

## 用途与边界

为用户提问、模型阶段输出和最终输出准备可检索的向量记录，通过任务节点和来源标识关联原始内容。树结构与恢复规则见[问答树存储](question-tree.md)。

本表提供数据契约、二进制编码、MySQL 迁移及仓库读写；另已提供 [MyVector 检索与 vLLM 适配](../../capability/infrastructure/myvector.md)，复用本表作为权威向量来源。[节点落盘层](node-persistence.md)已在保存公开消息时原子登记 pending 记忆，并独立保存完整原生生成；尚未接入问答运行时或自动向量化。

---

## 字段与关联

[002-question-node-memories.sql](../../../schema/002-question-node-memories.sql)在 `001-question-tree.sql` 之后手工执行，新增 `question_node_memories`。应用启动不会自动运行迁移，本次不修改真实数据库或配置。

| 字段 | 含义 |
| --- | --- |
| `id` | 32 位小写十六进制记忆 ID，由后端生成。 |
| `node_id` | 外键关联 `question_nodes.id`；仓库只允许任务节点登记记忆。 |
| `source_id` | 原始提问/消息的不可变来源标识，最长 128 字符，区分大小写。 |
| `kind` | `question`、`intermediate`、`final`。 |
| `embedding` | 小端连续 float32 二进制向量，`MEDIUMBLOB`，未成功时为空。 |
| `embedding_status` | `pending`、`success`、`failed`。 |
| `created_at` | 登记时间，UTC。 |
| `embedded_at` | 向量成功写入时间，UTC，未成功时为空。 |

不保存 `text`、`content_hash`、`embedding_model`、`embedding_dimensions`。来源原文由上游保存，本表不复制；`(node_id, kind, source_id)` 唯一约束保证同一来源只登记一次。树和用户归属经节点 JOIN 获取，不增加冗余字段。

新节点使用不可变 `NodeMessage.id` 作为 source_id，通过 `MySQLNodeRepository.load_memory_source` 回查原文并校验所有权。旧底层 register_memory 自定义引用可能无法解析，不能假定已登记即存在原文。流式片段不能作为完整消息登记。

interrupted 输出不登记记忆。用户停止后，该节点已有中途/最终输出的记忆记录保留审计，但检索候选、刷新与来源回查将其排除，问题仍可保留；停止、替代和失败的上下文差异统一见[节点生命周期](../workflow/question-answering/node-lifecycle.md)。

---

## 编码与状态一致性

[QuestionNodeMemory](../../../app/model/question_memory.py)拒绝空向量、字符串或布尔分量、NaN、无穷和超出 float32 范围的数值。写入前统一转换为 float32，读取及重复提交比较采用相同精度。记录不强制归一化，也不额外存储维度；维度可由字节长度除以 4 推导，但这不能证明模型一致性。

`success` 必须同时具有非空向量和 `embedded_at`；`pending`、`failed` 的两者均为空。SQL CHECK 限制二进制非空、长度为 4 的倍数及状态一致性，程序额外校验数值有限性，损坏记录读取时失败，不作为合法向量使用。

Embedding 模型及维度由调用方统一配置，入口和客户端见 [MyVector 配置](../../capability/infrastructure/myvector.md#配置与生命周期)。存储层不能识别同维度的不同模型向量；将来更换模型须统一重建，不能将两个模型的输出混用。`MEDIUMBLOB` 是权威存储格式，不是插件编码；独立的 `question_memory_index` 缓存通过 MyVector 编码并构建 HNSW 索引，不向本表补回已删除字段。

---

## 仓库接口与并发

复用 [MySQLQuestionTreeRepository](../../../app/repository/question_tree.py)的连接池、用户归属校验、超时及事务生命周期，不额外建立后台任务或连接池。

| 方法 | 行为 |
| --- | --- |
| `register_memory(tree_id, owner_userid, node_id=..., source_id=..., kind=...)` | 幂等登记 pending 记录，重复来源返回已有记录，包括已有成功结果。 |
| `list_node_memories(tree_id, owner_userid, node_id)` | 读取本人指定任务节点的记忆，按创建时间和 ID 排序。 |
| `complete_memory(tree_id, owner_userid, memory_id, embedding)` | 将 pending/failed 记忆写为 success；同向量重复完成幂等，不同结果拒绝覆盖。 |
| `fail_memory(tree_id, owner_userid, memory_id)` | 将未完成记录标记 failed；成功结果不能降级。 |

所有方法都需要上游传入已认证的用户标识，不接受凭来源 ID 绕过树归属。摘要节点不能登记记忆，跨树记录不能读写。登记依赖唯一键，写后当前读取得并发赢家；完成和失败更新先锁定记录再校验状态。冲突抛出 `QuestionTreeConflict`，不自动重放；异常、取消及超时复用仓库事务回滚规则。

记忆加工不更新树的主干、工作区、活动时间或 revision。向量服务故障时，上游可以只标记记忆失败，不改变已完成问答的终态；源消息回查由节点落盘层提供；自动调度、批处理重试及模型更换重建暂未实现。

仓库还提供 `list_searchable_memories(tree_id, owner_userid)`，只读取本人指定树内成功记忆，供检索前后刷新和回查；依赖失败不能作为空列表返回。插件索引不参与问答树结构加载。

---

## 验证

[离线模型测试](../../../tests/test_question_memory.py)验证移除字段被拒绝、三种来源类型、float32 小端往返、非法数值、损坏二进制和状态约束。[独立 MySQL 测试](../../../tests/test_question_tree_mysql.py)新增记忆测试，验证迁移、原始字节、唯一键、并发幂等、失败恢复、完成防覆盖、用户/树/摘要边界与 SQL CHECK。

实库测试只允许独立一次性容器和显式端口，不读取真实 `.env`，不连接默认 3306，不挂载 `data/`。套件会删除临时测试库的七张问答业务表，执行方法和环境约束见[问答树验证](question-tree.md#验证方式)。测试不调用在线 Embedding 或共享中间件。

2026-10-21 验证结果：新增 6 项离线模型测试和 6 项 MySQL 记忆测试通过；完整回归 213 项通过，无跳过（包含共 18 项 MySQL 8.4.11 隔离实库测试）。`pip check` 无冲突，新改文档 33 个相对文件链接通过。临时容器验证完成后清理，不代表已执行业务库迁移。

检索缓存通过[005 迁移](../../../schema/005-question-memory-fulltext.sql)保存 text 原文副本并建立 ngram 全文索引，权威记忆表仍不增加 text 字段。刷新及关键词查询见[全文检索](../../capability/infrastructure/fulltext.md)。
