# 问答树存储与内存索引

## 用途与当前边界

本模块准备问答树的 MySQL 存储、用户归属隔离、内存结构和上下文路径解析，已供 Agent Core 的内部生成入口读取和追加。应用按需连接仓库，现有登录、头像及平台接口保持原有行为。

数据库连接复用 [MySQL 配置](../../../app/config/database.py)及 asyncmy 选型。仓库只在显式调用时建立连接，不自动创建表，不发起中间件请求。本次开发不修改真实 `.env`，也不向现有业务库执行 DDL。

---

## 树、节点与摘要语义

树按用途分为 `assistant`（个人 AI 助理）和 `exploration`（复杂任务探索）。`assistant` 是探索模式的简化使用方式，两者共用树、节点、摘要、工作区和记忆存储，SQL 不另加禁止分支的拓扑约束。助理生成及节点仓库的助理入口只允许从最新主干末端继续，采用[压缩窗口驻留与单份工作区](../workflow/question-answering/assistant-memory.md)，拒绝节点选择或分支；通用追加/结束入口拒绝助理写入，助理必须使用专用线性任务和检查点接口。探索模式允许选择历史节点继续。当前 HTTP 仅开放 exploration。

每棵树属于一个用户；每个任务节点表示一轮用户问题及执行记录。摘要是独立的内部节点，不是覆盖前一个任务的标签。所有节点只有一个父节点，父链决定上下文，不按创建时间、主干或分支标识推测继承关系。

节点另以 `path_type` 区分主干（`main`）和分支（`branch`），与 `kind` 的任务/摘要分类独立：任务和摘要都可以位于主干或分支。主干为树根至 `main_leaf_id` 的父链，其他路径上的节点为分支；公共祖先仍为主干节点，不因另开分支而被重新分类。该字段不替代 `parent_id`，上下文仍按真实父链回溯。仓库在持树行锁的追加事务内统一计算标记，不采纳调用方的主干/分支声明；加载结构时校验标记与主干父链一致，损坏记录明确失败。

```text
S1 → C → D
         ├→ S2 → E
         └→ S3 → H
```

| 选择的节点 | 恢复的上下文 |
| --- | --- |
| D | S1、C、D |
| E | S2、E |
| H | S3、H |

解析从所选节点开始，遇到第一个摘要即停止；没有摘要时回溯至根，再将任务按祖先顺序排列。摘要生成、压缩阈值和新问题注入由后续运行时负责，这里只返回需要读取的节点及正文，不发起模型请求。

新摘要记录实际覆盖的连续区间。当前基础设施支持将所选路径的完整有效上下文压缩成一个摘要：覆盖起点为最近旧摘要或根，终点为新摘要的直接父节点；新摘要自身不覆盖后续任务。后续若需要只压缩路径前半部分并保留其后的任务，需另行定义分支路径投影，不能修改已有节点父链。本轮不引入该额外行为。

---

## MySQL 数据结构

迁移文件为 [001-question-tree.sql](../../../schema/001-question-tree.sql)，包含四张 InnoDB 表。

另有 [002-question-node-memories.sql](../../../schema/002-question-node-memories.sql)新增节点向量关联表，字段和使用边界见[节点记忆向量存储](question-memory.md)。

[003-question-memory-index.sql](../../../schema/003-question-memory-index.sql)另建 MyVector 可重建缓存，仅在部署插件并显式接入检索时使用，不改变树的父链、摘要或工作区版本。七张业务表与一张索引缓存分开管理，检索说明见 [MyVector 适配](../../capability/infrastructure/myvector.md)。

| 表 | 主要字段与职责 |
| --- | --- |
| `question_trees` | `id`、`owner_userid`、`name`、`kind`（006）、`main_leaf_id`、`revision`、UTC 创建与活动时间；用于按用户和用途列树。 |
| `question_nodes` | `id`、`tree_id`、`parent_id`、`kind`、`path_type`（007）、`status`、`sequence`、`workspace_version_id`、创建时间；只保存轻量结构。 |
| `question_node_contents` | `node_id` 与 JSON 正文；任务保存问题、已接受交互和回答，摘要保存文本与覆盖起止节点。 |
| `question_workspaces` | 独立不可变工作区版本，保存 `id`、`tree_id`、JSON 内容与创建时间；节点引用版本。 |

[006-question-tree-kind.sql](../../../schema/006-question-tree-kind.sql)为树表补充用途字段，不新增业务表：

| 字段 | SQL 定义 | 语义 |
| --- | --- | --- |
| `kind` | `ENUM('assistant','exploration') NOT NULL DEFAULT 'exploration'` | `assistant` 为个人助理，`exploration` 为复杂任务探索；已有树默认归为探索类型。 |

新增索引 `(owner_userid, kind, updated_at, id)`，仓库已支持按用户、用途列树；原有按用户列树的索引保留。类型字段表达用途；探索保留选点、快照继承与分支规则，助理由专用服务和仓库强制线性追加，使用[当前工作区与压缩窗口](../workflow/question-answering/assistant-memory.md)。后续 [011 助理存储迁移](../../../schema/011-assistant-storage.sql)另加仅作用于 assistant 树的用户唯一索引及助理绑定外键，落实每位用户至多一棵助理树；探索树数量不变。迁移前须核查历史重复树，完整约束和当前实现范围见[助理存储](assistant-storage.md)。

[007-question-node-path-type.sql](../../../schema/007-question-node-path-type.sql)为节点表补充路径类型，不新增表或索引；全树读取仍使用现有树内序号索引。

| 字段 | SQL 定义 | 语义 |
| --- | --- | --- |
| `path_type` | `ENUM('main','branch') NOT NULL DEFAULT 'main'` | 主干或分支归属，与 `kind` 的 `task` / `summary` 独立。 |

例如根节点 A 后主干为 `S1 → C → D → S2 → E`，从 D 另开 `S3 → H` 时，A、S1、C、D、S2、E 为 `main`，S3、H 为 `branch`；从 H 继续的节点仍为分支节点。节点归属、摘要、正文、工作区和主干指针在同一事务内提交。本次不新增主干切换或分支提升操作。

**迁移状态：** 模型、仓库、上下文加载与生成服务已适配，测试使用包含 `006`、`007` 的新结构，未执行真实业务库迁移。`001` 保留基础定义，新库也需补执行 `006`、`007`，旧应用不能提前使用新增字段（其模型禁止额外字段，仓库使用 `SELECT *`）。`007` 的默认 `main` 不会识别历史分支，非空旧表仍需在部署前准备按 `main_leaf_id` 父链回填主干、其余节点标记分支的配套迁移；不能把默认值视为历史分类结果，应用也不会隐式回填或修复。

当前节点模型还要求 [008 生命周期迁移](../../../schema/008-question-node-lifecycle.sql)，新增 superseded、结束时间、同树接替关联和消息完成/中断状态。字段、终态控制与派生标记统一见[节点生命周期](../workflow/question-answering/node-lifecycle.md)，不由客户端分别维护状态与上下文标签。

ID 由后端生成，为 32 位小写十六进制 UUID。用户标识区分大小写，不外键关联 `employees`，以支持已认证的开发模拟用户。底层工作区以 JSON 对象存储，Agent 入口按[四区域工作区契约](../workflow/question-answering/context-management.md#四区域工作区)校验；不把任意旧 JSON 当作可用工作区。

树列表索引为 `(owner_userid, updated_at, id)`；节点有 `(tree_id, sequence)` 唯一索引和 `(tree_id, parent_id)` 索引。同树复合外键限制父节点及工作区不能跨树。单根、递增父子顺序、主干末尾为任务节点等规则由仓库和内存快照在事务内校验；不依赖调用方直接操作 SQL。`main_leaf_id` 不建立反向外键，以避免建表循环依赖。

部署时先选定问答业务的目标 schema，再手动执行迁移文件；文件不包含 `CREATE DATABASE`，也不使用 `IF NOT EXISTS` 隐藏已存在的结构差异。当前仓库使用现有 `MYSQL_*` 配置，默认值或人员表所在库不代表本轮已经建表或迁移；不要在 FastAPI 启动时自动运行迁移。

---

## 内存构树与正文加载

[QuestionTreeSnapshot](../../../app/service/question_tree.py)一次接收树元数据和全部节点元数据，构建：

- `nodes_by_id`：通过 ID 获取节点。
- `children_by_parent`：通过父节点获取有序子节点列表。
- `nodes`：按树内 sequence 排列的元数据序列。

索引以只读映射和元组暴露，节点模型不可变。仓库按 sequence 排序读取，因此内存索引构建时间与结构占用为 O(N)；传入无序数据时额外进行 O(N log N) 排序。构建时校验重复 ID/序号、缺失父节点、跨树、多个根、环、生成中父节点与非法主干指针；严格递增父子序号排除循环，遍历不依赖 Python 递归栈。

`context_path(node_id)` 返回最近摘要、其后的任务序列及所选节点的工作区引用，回溯成本为 O(h)。所选节点之后的摘要和其他分支均不会被使用；生成中的节点不能作为继续提问的起点。取消、被替代和失败节点可作为显式新问题起点，模型上下文按终态过滤半成品并附带业务结束标记，不把原始正文和私有生成直接发送给模型。

底层 `load_tree()` 只读取树与节点结构，不加载正文。`load_context()` 在同一个 REPEATABLE READ 事务内读取结构、批量获取选中路径正文及对应工作区，避免结构和正文来自不同提交。`load_exploration_tree()` 在同一事务内批量加载本人探索树的完整结构、全部节点正文与公开消息；树类型不是 exploration 时拒绝，不读取原生生成、向量或工作区正文。公开展示过程另存 `question_node_contents.display_history`，随打开事务读取，字段与迁移边界见[探索任务展示恢复](../workflow/question-answering/exploration-session.md#sse-与恢复)。完整前端响应见[打开问答树](../../api/exploration.md#打开问答树)，不以最近摘要路径代替完整历史。


[ExplorationTreeService](../../../app/service/exploration_tree.py)与节点执行仓库共用 [QuestionTreeCache](../../../app/service/question_tree_cache.py)，驻留键为 `(owner_userid, tree_id)`，只保留已构建的树结构和节点索引，不另存展示响应副本。打开、注册/激活时的节点选择、上下文加载和节点追加均通过同一仓库取得这份结构。每次读取先在事务内查询权威树行，确认归属、用途及版本；同版本命中时直接返回同一个 `QuestionTreeSnapshot`，不重读全部节点或重建索引。正文、公开消息及所选工作区仍按各入口需要查询，模型注入范围保持最近摘要和其后的所选父链。

驻留采用进程内 LRU，默认最多 32 棵，由打开服务构造参数 `max_loaded_trees` 设置共享池容量；不新增环境变量。缓存缺失、淘汰或权威版本不一致时，才读取全体节点并构建结构。打开失败、删除及停机释放驻留；淘汰仅释放内存，不删除 SQL 或附件。多进程各自维护缓存，通过读权威树行发现其他实例的版本变更，不宣称跨实例共享内存。

创建、追加任务/摘要、检查点、终态、停止、替代和改名均根据本事务写入结果生成新的不可变结构快照，不重读历史节点；构建新版本的索引仍为 O(N)，旧读者持有的快照不被原地修改。事务内暂存结构仅对本事务可见，SQL 提交确认后才发布到共享池；回滚、取消或提交回执不确定时丢弃暂存并撤销相关驻留。较早读事务的迟到发布不能覆盖更高 revision，删除/清理前开始的事务不能重新驻留旧树。替代任务的临时取消状态不进入共享池。私有审计追加和 SSE 展示记录不改变模型结构版本，继续按原规则落库。

内部 `get_loaded_snapshot(tree_id, owner_userid)` 返回该共享池中的当前驻留结构，不独立提供授权或跨进程新鲜度保证；业务执行必须通过上述带权威树行校验的仓库入口。同一用户树的打开与删除仍共用进程内操作锁。完整展示正文读取、拓扑校验与前端投影按节点/消息数量线性处理，响应保持平铺节点数组。

创建模式与节点类型分开：推进模式生成 main 主干节点，仅允许延续当前主干末尾；探索模式生成 branch 分支节点，可从任意已结束的主干或分支任务继续，不改变主干末尾。已有追加事务从父链推导 path_type，忽略调用方伪造的标签，并拒绝从分支节点推进主干。接口不增加派生的“可开启分支”字段。

---

## 仓库使用与写入一致性

[MySQLQuestionTreeRepository](../../../app/repository/question_tree.py)提供以下内部方法，不是 HTTP API：

| 方法 | 行为 |
| --- | --- |
| `create_tree(owner_userid, name, kind=...)` | 创建空树，默认名称为“新的问答树”，默认用途 `exploration`。 |
| `list_trees(owner_userid, limit, offset, kind=None)` | 按活动时间和 ID 降序读取本人树；可筛选用途，默认不筛选；默认 50 条，最大 200 条。 |
| `list_exploration_trees(owner_userid)` | 按活动时间和 ID 降序读取本人全部 exploration 树的 `id/name/updated_at`（带时区的 UTC 时间），SQL 排除 assistant，不加载节点或工作区。供[探索列表接口](../../api/exploration.md#获取问答树列表)使用。 |
| `rename_exploration_tree(tree_id, owner_userid, name)` | 供[探索树改名接口](../../api/exploration.md#修改问答树名称)使用；同事务行锁校验归属和用途，修改名称时递增版本、更新活动时间，同名不写入。 |
| `rename_tree(..., expected_revision)` | 校验归属与版本后改名，更新 revision。 |
| `load_exploration_tree(tree_id, owner_userid)` | 同一事务校验本人 exploration，复用同版本结构并读取全部正文与公开消息，校验缺失正文及摘要范围，返回 LoadedQuestionTree。 |
| `load_tree(tree_id, owner_userid)` | 校验本人树行，探索树复用同版本驻留结构，未命中时构建快照。 |
| `append_path(..., expected_revision, advance_main, workspaces)` | 追加连续节点路径，支持“摘要→任务”同事务提交；创建新版本时一并写入。 |
| `finish_task(..., expected_revision, status, content, workspace)` | 将生成中任务一次转换为完成、取消或失败，可提交新的工作区版本。 |
| `load_context(tree_id, owner_userid, node_id)` | 返回路径、该路径正文和所选节点工作区，尚不组装模型消息。 |
| `close()` | 等待连接池关闭；关闭后禁止重新打开。 |

所有树访问都要求调用方提供服务端已认证的用户标识；本仓库不验证 access_token。非本人或不存在的树统一抛出 `QuestionTreeNotFound`，不暴露他人树资料。

写入先锁定树行，再校验 `expected_revision`，冲突抛出 `QuestionTreeConflict`，不自动重放。sequence 在树内递增，但不决定上下文；首条路径建立主干，后续仅在 `advance_main=True` 且从原主干末尾延伸时更新主干指针，并将该批节点设为 `main`。其他追加设为 `branch`，不会修改原后继、归属、正文、摘要或工作区版本。`start_task` 默认按父节点是否为主干末端选择是否推进，分支后续仍属于分支；摘要和任务接受同一次路径归属计算。

探索节点未指定新工作区时继承直接父节点的版本；指定版本只能是父节点版本或本次提交的新版本，不能借用另一分支的现有工作区。生成中任务结束时可创建新版本；禁止修改原问题或重新结束终态任务。活动任务逐工具检查点与用户取消投影见节点落盘及生命周期；崩溃后自动恢复仍未实现。

节点、正文、工作区、主干指针与 revision 的变更在同一个事务提交。数据库故障、超时和取消均向上传播并尝试回滚；提交回执丢失仍可能存在结果不确定性，调用方应重新读取，不盲目重试。任务附件 UUID 与类型随正文保存，见[附件登记与定位](question-file.md)。整树级 HTTP 删除及探索任务 PDF 上传已接入，不改动人员权限。

---

完整消息、原生生成记录与事务检查点使用[节点落盘与恢复](node-persistence.md)，新增 [004 迁移](../../../schema/004-node-persistence.sql)。本页底层结构接口不替代运行日志接口。

---

## 整树删除与附件清理

入口为[删除问答树 API](../../api/exploration.md#删除问答树)。[QuestionTreeDeletionRepository](../../../app/repository/question_tree_deletion.py)复用树仓库事务；[ExplorationTreeService](../../../app/service/exploration_tree.py)协调 SQL、进程内快照及 [UploadFileStore](../../../app/infrastructure/upload_file.py) 文件删除。

删除前以已认证 userid 锁定树行并校验 `kind='exploration'`。不存在、非本人或助理树统一拒绝。读取本树任务正文中的 attachments，按 UUID 去重；类型冲突或其他树也引用同一 UUID 时返回冲突，保留数据及文件。正常附件入口本身不允许跨树借用 UUID，此检查保护历史异常登记。

删除顺序：

1. 在同一个数据库事务内创建待清理清单，删除已部署的 `question_memory_index` 中本树缓存，再删除节点向量、公开消息、原生生成和正文。
2. 在该事务内解除节点的父子及接替节点自引用，再删除全部节点、全部工作区版本和树记录。解除引用时临时同步调整 kind/status 以满足现有 CHECK；这些中间状态不会单独提交，故障会整体回滚。不禁用外键约束，不依赖删除顺序或级联深度。
3. SQL 提交后移除本进程内快照，逐个清理已登记的固定 UUID 文件。文件缺失按已清理处理；符号链接、非普通文件或 I/O 错误明确失败。
4. 文件全部清理后删除待清理清单，才向 HTTP 返回成功。

| 表 | 清理范围 |
| --- | --- |
| `question_trees` | 本人指定探索树 |
| `question_nodes` | 本树全部主干、分支、摘要、生成中及已结束节点 |
| `question_node_contents` | 本树节点正文，包括附件引用和工具交互 |
| `question_workspaces` | 本树所有工作区版本，包括不再被节点引用的旧版本 |
| `question_node_messages` | 本树节点全部公开消息，包括中断输出 |
| `question_node_generations` | 本树节点全部原生生成与推理记录 |
| `question_node_memories` | 本树节点向量及待向量化记录 |
| `question_memory_index` | 已部署时删除本树向量/全文检索缓存；未部署该可选表时跳过 |
| `question_tree_deletions` | 文件清理全部成功后删除本次清单 |

[009-question-tree-deletion.sql](../../../schema/009-question-tree-deletion.sql)新增清理清单表，字段为 `tree_id`、`owner_userid`、`attachments` JSON、`created_at`。它没有指向树的外键，以便树删除后仍能恢复文件清理；仅短期保存待清理引用，不作为另一份树或附件归属表。部署须手工执行此迁移，应用不自动建表。已有 001–008 的存储定义保持不变。

数据库和文件系统不具备共同事务。SQL 阶段失败不会触碰文件；SQL 已提交后，文件错误、清单删除失败、取消、响应丢失或进程退出，均可通过原用户重试相同 DELETE 继续完成，不能把缺失的树误当成清理完成。数据库提交结果不确定时同样保留可重试语义。成功清理后重复请求返回不存在；不新增后台轮询或孤立文件扫描。

跨进程的普通节点写入/改名与删除共用数据库树行锁。`QuestionMemorySearch.refresh/search` 在发布索引前通过 `memory_index_write_scope` 再次锁定权威树行，防止先前取得的旧快照在删除后重写检索缓存；搜索返回前仍回查权威来源。底层 MyVectorStore 是可信基础设施，直接写入方必须遵循相同锁契约，不能绕过服务层发布。索引锁持有期间沿用数据库操作超时约束。

进程内打开/删除使用同一键锁；删除开始后的失败或取消也释放本地旧快照。其他实例可能仍保留此前打开的内存快照，但 HTTP 每次打开都会重新查询数据库，不能以缓存命中恢复已删除树。已装配探索 HTTP 生成任务；本进程删除先取消并等待生产者，再删除 SQL 与附件，注册和激活复用同一树操作锁。跨进程生成/删除仍需部署侧协调，不能仅凭数据库删除推断其他实例已停止模型推理。尚未登记的孤立上传无法从树推断归属，不扫描真实目录清理。

删除专项验证见[接口与并发测试](../../../tests/test_exploration_delete.py)、[文件删除测试](../../../tests/test_upload_file.py)、[隔离 MySQL 验证](../../../tests/test_exploration_delete_mysql.py)。实库测试在独立临时库执行迁移，覆盖自引用外键、全表清理、其他树隔离、SQL 回滚、清单重试和旧索引发布拒绝；文件显式放在 tests/tmp。

---

## 验证方式

2026-10-27 驻留结构复用验证见 [共享缓存测试](../../../tests/test_exploration_structure_cache.py)：显式启用 `RUN_ISOLATED_TREE_CACHE_MYSQL=1`，共 7 项通过（2 项缓存单元、5 项随机临时 MySQL 库集成），临时库在结束后删除。测试先清空缓存并打开一棵树，记录一次全节点查询，再禁止该 SQL；随后重复打开、校验选择、加载上下文、追加任务、检查点、终态、摘要及分支均成功，全节点查询仍为一次，同版本结构通过对象身份断言确认复用。另覆盖替代/取消/失败、改名、事务暂存不可见、回滚和取消、旧读事务晚返回、跨实例版本失配、归属、LRU 与删除。常规回归在新增最后一项取消测试前执行：210 项中 194 项通过、16 项跳过；新增测试后单独重跑上述 7 项全部通过。`git diff --check` 通过。验证未调用真实模型，未修改业务数据库或重启服务；不代表实际用户请求的性能测量。

打开接口与内存驻留测试见[完整树打开测试](../../../tests/test_exploration_open.py)，覆盖完整分支历史、公开投影、认证隔离、并发版本防倒退、LRU 与停机清理、2500 层树序列化。

离线测试见 [内存树测试](../../../tests/test_question_tree.py)和[仓库边界测试](../../../tests/test_question_tree_repository.py)，覆盖分支摘要隔离、D 不消费后续摘要、无摘要回溯、5000 层深树、结构损坏、用户查询隔离、超时取消及关闭。

[MySQL 隔离测试](../../../tests/test_question_tree_mysql.py)默认跳过。仅在独立一次性 MySQL 8.4 容器中，以固定测试库 `knowledge_question_tree_test`、测试密码及显式随机宿主机端口执行；拒绝默认 3306 端口，不加载真实 `.env`。基础套件执行 001、002、004、006、007、008、010 七份迁移；删除专项另执行 003、005、009，验证可选索引与清理清单。测试会重建临时库中的对应表，仅可用于一次性容器。容器数据不挂载项目 `data/` 或业务目录。

```bash
python -m unittest tests.test_question_tree tests.test_question_tree_repository -v
# 端口替换为独立临时容器映射的端口；不是业务 MySQL 端口。
QUESTION_TREE_TEST_MYSQL_PORT=独立临时端口 python -m unittest tests.test_question_tree_mysql -v
```

实库验证包括迁移 DDL、JSON 往返、大小写归属、主干及分支恢复、工作区版本隔离、节点/正文/工作区事务回滚、并发旧版本冲突、跨树外键、终态防重放和损坏正文拒绝。测试不调用共享中间件或在线模型，不代表生产迁移与问答运行时已经完成。

2026-10-21 本地验证：新增 20 项离线测试与 12 项独立 MySQL 8.4.11 实库测试均通过；携带独立容器端口运行完整回归，共 201 项通过，无跳过，`pip check` 无依赖冲突。新改文档的 28 个相对文件链接校验通过。临时 MySQL 仅使用容器临时卷，验证结束后清理；真实数据库、头像和配置未改动。

树用途与路径类型适配后，[专项测试](../../../tests/test_question_tree_types.py)新增 4 项离线和 8 项独立 MySQL 测试，覆盖字段校验、用户与用途筛选、摘要/分支归属、重连后的快照恢复、调用方伪造标记拒绝、助理自动起点与摘要、完整输出/推理落盘、失败起点一致性、并发冲突及损坏标记拒绝。完整回归 351 项通过，无跳过，包括 61 项独立 MySQL/MyVector 验证；`pip check` 无冲突。所有模型及外部身份请求均使用隔离替身，不代表真实模型端到端性能验收或业务库迁移完成。

探索任务注册和首次订阅激活现已接入，见[任务生命周期](../workflow/question-answering/exploration-session.md#停止删除与生命周期)。整树删除在本进程先取消并等待生产者，再删除 SQL 与文件；跨进程协调不由进程锁保证。

---

## 主题摘要正文编码

新摘要在 `SummaryContent.text` 中序列化保存主题记忆，旧纯文本仍可读取；不新增 SQL 列或改写既有记录。打开树的公开 `content.text` 保持可读正文，模型上下文同样投影主题和业务来源。完整格式、验收和后续整理规则见[主题发现与总结](../workflow/question-answering/topic-summary.md#存储与注入)。
