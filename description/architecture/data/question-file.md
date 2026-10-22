# 用户附件引用与文件定位

## 用途与边界

沿用 contract-service-be 的“UUID 文件引用、任务内登记、固定文件名定位”方式，为内部问答生成入口提供文件保存、引用恢复及按归属读取。仍使用本项目 MySQL 和 asyncmy，不引入 SQLite 或独立文件表。

文件存储本身不负责 HTTP 或格式转换；[探索任务 API](../../api/exploration.md#注册探索任务)已接入直接上传 PDF，首次 SSE 激活后校验实际内容、落盘并登记。[资源读取 API](../../api/resource.md#读取任务附件)已接入，通过中央身份及树内登记取得原始附件；普通用户只可读取本人树内附件。模型引用展示见[上下文渲染](../workflow/question-answering/context-rendering.md#当前任务附件)；Core 已独立接入[图像逐页阅读](../workflow/question-answering/image-reading.md)，音频和视频暂不支持查看。

---

## 唯一引用与固定路径

[AttachmentReference](../../../app/model/question_file.py) 只有两个持久化字段：

| 字段 | 规则 |
| --- | --- |
| `file_id` | 后端以 UUID4 生成，完整小写、带连字符；不接受路径、文件名或缩写。 |
| `type` | `images` 多图、`audio` 音频、`video` 视频。 |

| 类型 | 固定后缀 | 文件位置 |
| --- | --- | --- |
| 多图 | `.pdf` | `data/user/upload-file/<UUID>.pdf` |
| 音频 | `.mp3` | `data/user/upload-file/<UUID>.mp3` |
| 视频 | `.mp4` | `data/user/upload-file/<UUID>.mp4` |

后缀由已登记类型推导，不另外保存路径、原文件名、摘要或哈希，也不让调用方提供服务器路径。模型只看到语义类型和 UUID，具体格式与目录不注入。

引用保存在 `question_node_contents.content` 的 `TaskContent.attachments` 中，与节点、问题和初始工作区在同一事务提交；无需新增 SQL 表或列。无附件的既有任务读取为空列表。单个任务不允许重复 UUID；检查点、终态和相同节点 ID 重放不能删除或替换原附件。

---

## 内部使用

文件存储由 [UploadFileStore](../../../app/infrastructure/upload_file.py) 提供，目录默认为 `data/user/upload-file/`；构造对象不创建目录或读取真实配置。单文件默认上限 50 MiB，可通过构造参数调整，不增加 `.env` 配置。文件原样保存，不根据语义类型转码或确认其内部格式；图像查看器另验真实格式、密码和页面可读性。

[QuestionFileService](../../../app/service/question_file.py) 负责关联仓库：

| 方法 | 行为 |
| --- | --- |
| `prepare(tree_id, owner_userid, uploads=..., references=...)` | 整批验收输入及树归属，核实已有引用属于本树，然后为新文件生成 UUID 并落盘；返回等待任务登记的引用。 |
| `read_file(tree_id, owner_userid, file_id)` | 从该用户的指定树任务正文查出登记类型，读取固定后缀文件，结束后再次核实登记；返回引用与字节。 |

应用组合根为资源路由与问答生成复用同一个文件服务；读取接口不加载模型，完整 HTTP 契约见[读取任务附件](../../api/resource.md#读取任务附件)。

UUID 不是访问凭据。没有任务登记的磁盘文件、其他用户或其他树的文件均不能经文件服务读取。仓库按 UUID 精确查询任务 JSON；同一 UUID 的登记类型若不一致则拒绝读取。磁盘操作不持数据库锁，读取后校验防止继续使用已经删除或失效的登记。

```python
from app.infrastructure.upload_file import UploadFileStore
from app.service.question_file import QuestionFileService
from app.service.question_agent import QuestionAgentService

# nodes、core 与 user 由宿主按既有方式装配；调用方已将多图整理为 PDF。
files = QuestionFileService(nodes, UploadFileStore())
agent = QuestionAgentService(nodes, core, file_service=files)
result = await agent.generate(
    user=user, tree_id=tree_id, selected_node_id=selected_node_id,
    question="请阅读这些材料。",
    uploads=[{"type": "images", "content": pdf_bytes}],
)
restored = await nodes.load_node(tree_id, user.userid, result.node_id)
reference, content = await files.read_file(
    tree_id, user.userid, restored.content.attachments[0].file_id,
)
```

探索与助理 `build_service(..., file_service=files)` 同样支持注入。`generate(attachments=...)` 用于引用已经登记在本树的文件；`uploads=...` 用于新字节，两者可同时提供。只使用普通文本时无需装配文件服务；提供附件而未装配则在停止旧任务前失败。历史任务恢复各自附件，不将当前附件填入其他任务；摘要回溯范围仍由树上下文决定。

---

## 落盘、失败与安全边界

本项目采用“先完整保存文件，再原子登记任务”，不等待会话归档；读取直接来自磁盘，不建立第二份常驻字节缓存。新文件以独占方式创建、权限为 `0600`，写入后同步；UUID 碰撞不会覆盖既有文件。根目录各路径分量和目标文件均拒绝符号链接；读取只接受非空普通文件并限制字节数，FIFO 不会造成阻塞。删除仅使用已校验 UUID 及固定类型后缀、目录描述符和不跟随链接的文件检查，不递归删除目录。

文件系统与 MySQL 不具有共同事务。数据库失败、提交回执不确定、取消或进程退出时，可能保留尚未登记的 UUID 文件；它们不能被读取服务授权。一批文件部分保存失败时补偿已经保存的文件；任务登记前失败时，仅在数据库明确确认 UUID 未登记后补偿新文件；SQL 响应不确定时不盲删文件，避免误删已经提交但丢失回执的附件。已有任务附件不因重新登录、失败或取消而自动删除。[整树删除](question-tree.md#整树删除与附件清理)已接入，按任务正文收集已登记附件、数据库提交后逐个删除。`UploadFileStore.delete(reference)` 由清理编排调用，缺失文件视为已清理，不创建目录，拒绝符号链接和非普通文件；不读取文件正文，因此可清理空文件或超限残片。未登记孤立文件仍无法归属到具体树，本模块不做目录扫描或推测删除。

`prepare` 是可信宿主接口，返回后应通过 `generate` 或仓库创建任务完成登记，不能将其返回视为登记已经成功；底层 Store 只提供文件 I/O，用户可访问入口须经过文件服务。前端身份仍由中央 token 验证后传入，不从 UUID 推断用户。

---

## 验证

[文件存储测试](../../../tests/test_upload_file.py)使用 `tests/tmp/`，覆盖三类固定后缀、重新构造读取、独占写、符号链接、路径参数、普通文件/大小限制、部分写入清理、权限校验及读取后的再次核验。[隔离 MySQL 测试](../../../tests/test_question_file_mysql.py)覆盖 JSON 登记与恢复、检查点保留、历史渲染、跨用户/树隔离、稳定 ID 重放、原子替代、未登记文件拒绝、Agent 实际请求及 SQL 失败。运行方式沿用[问答树隔离测试](question-tree.md#验证方式)。

2026-10-22 验证：新增 10 项存储/文件服务测试和 9 项隔离 MySQL 测试；完整 449 项通过、无跳过，其中 93 项使用独立临时 MySQL/MyVector。覆盖生成失败后的原附件保留、显式调整方向的新旧附件隔离，以及模型调用发生前附件已经登记。pip check 通过；文档本地文件链接 330 个有效。测试容器和临时文件已清理，未修改实际 `.env`、共享数据或其他项目，未请求真实模型或业务服务。
