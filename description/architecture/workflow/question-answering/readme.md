# 企业问答智能体

## 当前范围

已实现通用异步 Agent Core、必要工作区与输出工具、受控压缩、问答树上下文装配和节点持久化。参考 contract-service-be 的注册表、单工具循环、四区域工作区、上下文预算；不迁入合同门禁、合同检索、日期、程序执行或其他业务工具。

已接入[网页搜索与正文读取](web-search.md)，支持三工具、精炼子图与自动折叠；已接入[外部专家问答](external-expert.md)，复用源项目首次求助、追问与 LRU 会话契约。当前提供内部 Python 调用及[探索任务注册与 SSE](exploration-session.md)，已支持 HTTP PDF 上传、任务附件登记、归属读取、图像逐页阅读及停止/改变方向的独立控制；音频/视频阅读尚未接入。模型及 SQL 按需连接，不自动执行数据库迁移。现有登录、头像、平台查询及中央 token 契约不变。

---

## 阅读入口

| 文档 | 内容 |
| --- | --- |
| [包结构与导出](package-structure.md) | 提示词、工具、预算、渲染及子 Agent 的组织和依赖。 |
| [助理与探索模式](agent-modes.md) | 模式工厂、专用工具扩展、个人背景注入及身份隔离。 |
| [核心循环](agent-core.md) | 输入、工具、校验、提交、输出、取消及故障。 |
| [上下文与工作区](context-management.md) | 最近摘要、分支恢复、token 预算与压缩。 |
| [上下文渲染与消息](context-rendering.md) | 参考项目的任务/工作区展示、独立消息及原生历史。 |
| [节点落盘](../../data/node-persistence.md) | 消息、原生生成及快照的事务与恢复。 |
| [附件引用与文件定位](../../data/question-file.md) | UUID、固定后缀、任务登记、磁盘保存与归属读取。 |
| [工具注册、返回与折叠](tool-content-contract.md) | 返回类型声明、执行事实、页面解析、自动折叠及提交边界。 |
| [网页搜索与正文读取](web-search.md) | 搜索、候选分页、正文精炼、LRU 与 SSE 动作提示。 |
| [外部专家问答](external-expert.md) | 专业求助、连续追问、独立 Responses 客户端与私有原生审计。 |
| [图像文件阅读](image-reading.md) | 单一图像工具、LRU 驻留、临时原生图片及提交边界。 |
| [节点生命周期](node-lifecycle.md) | 停止、方向调整、失败、完成及迟到写入保护。 |
| [可选工具格式注入](../../../capability/infrastructure/tool-tag.md) | 模型协议模板、装配快照、开关及分词/生成一致性。 |
| [提示词规范](../../../standard/prompt-engineering.md) | 模型视角、工具字段说明和协议。 |
| [上下文规范](../../../standard/agent-context-management.md) | 纠错生命周期和私有审计隔离。 |

---

## 主动检索与证据核实

公共角色提示词 `app/agent/core/prompt/role.py` 统一规定取证时机，探索与助理模式继承该规则。结论依赖公开事实且存在材料不足、时效变化、证据冲突或关键事实把握不足时，模型应主动使用已提供的网页检索工具，不等待用户明确要求搜索；用户目标、偏好及非公开材料的缺口则向用户澄清。

决定结论的事实需要阅读支持它的来源正文，搜索摘要和专家意见不自动等于已核实事实。最终答复前应尝试解决可检索的关键缺口，无法核实时保留限制；已有材料足够的整理、改写与推理不强制检索。专家仍使用首次咨询和继续追问两个工具，契约见[外部专家问答](external-expert.md)。

这属于模型行为提示，不增加强制搜索次数或程序执行门禁，也不保证模型每次都会选择正确工具。实际调用效果需要结合真实任务轨迹验证。

---

## 最小调用

```python
from app.agent.core import AgentCore
from app.config.agent import load_agent_core_settings
from app.config.mllm import load_mllm_settings
from app.infrastructure.mllm import MLLMClient
from app.repository.node_persistence import MySQLNodeRepository
from app.service.question_agent import QuestionAgentService

# trees 是已有 MySQLQuestionTreeRepository；user 是已认证身份。
settings = load_mllm_settings()
async with MLLMClient(settings) as client:
    core = AgentCore(settings, client=client, options=load_agent_core_settings())
    service = QuestionAgentService(MySQLNodeRepository(trees), core)
    result = await service.generate(
        user=user, tree_id=tree_id, selected_node_id=selected_node_id,
        question="请根据已有资料说明当前结论。",
        context_injection="本次补充材料。",
        thinking_effort="medium",
    )
```

空树首次生成 `selected_node_id=None`。非空 `exploration` 树显式选择摘要或终态任务作为起点，非成功终态按生命周期规则过滤历史；`assistant` 树省略该参数，由服务端选择主干末端，不接受调用方选点。可用 `trees.create_tree(user.userid, kind='assistant')` 创建助理树，省略 `kind` 则创建探索树；两者共用底层结构。树的创建和访问控制由调用方及已有仓库负责，不从用户文本猜测树身份。

核心能力可以单独用 `AgentCore.run(AgentInput(...), journal=MemoryJournal())` 在无数据库宿主运行。需要持久化时使用组合服务，不能把内存宿主的成功等同数据库提交成功。

需要专属角色、工具或个人背景时，使用 `app.agent.assistant.build_service` / `app.agent.exploration.build_service`，装配和 loader 契约见[模式说明](agent-modes.md)，不在 Core 循环中增加模式判断。
