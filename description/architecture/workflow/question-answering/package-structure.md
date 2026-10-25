# Agent Core 包结构

## 组织原则与入口

借鉴 contract-service-be 的 Agent Core 按提示词、工具、上下文预算、上下文展示和内部子 Agent 划分职责。本项目只组织已实现的能力，专家问答、网页搜索与正文读取已按源项目契约迁入公共 Core，文件查询只借鉴候选组合机制，不复制合同专用字段或类别对齐，也不为未来能力建立空目录。

稳定调用入口仍为 `app.agent.core`，导出 AgentCore、AgentInput、AgentResult、AttachmentReference、ContextInjection、HistoryTurn、CoreSettings、MemoryJournal 和 NodeJournal。外部服务通过入口使用，子功能通过各自 `__init__.py` 显式导出，不使用通配导入或重复实现。

助理与探索业务装配位于平级的 `app.agent.assistant`、`app.agent.exploration`，共用树公开投影位于 `app.agent.context`；目录与扩展契约见[模式包说明](agent-modes.md)。Core 不导入这些模式包。探索中途输出、开场/长执行提醒和 SSE 正文适配均位于探索包；通用 Core 只保留提交、交付及观察接口。

---

网页精炼子图位于 `subgraph/web_page/`，包含 HTTP、Trafilatura、模型精炼和唯一收束节点；规则见[网页工具](web-search.md)。

## 当前结构

```text
app/agent/core/
├── __init__.py                 # 稳定宿主接口
├── runtime.py                  # 主循环与生命周期
├── guidance.py                 # 模式请求提示协议，无沟通文案
├── output.py                   # 可选公开输出观察协议，无 SSE 实现
├── model.py                    # 内部任务与结果契约
├── workspace.py                # 共享四区域数据模型，不是工具实现
├── journal.py                  # 权威提交与私有生成
├── expert.py                   # 用户树专家池与共享客户端生命周期
├── web.py                      # 用户树网页搜索/正文资源及活动租约
├── json_codec.py               # 共用 JSON 传输兼容
├── context.py                  # 对外上下文装配入口
├── native_messages.py          # 已接受原生工具对校验与历史封闭
├── page_display.py             # 通用页面展示与自动折叠，独立于文件 LRU
├── file/
│   ├── __init__.py
│   ├── pdf.py                  # 真实多图检查与按视觉预算渲染
│   ├── cache.py                # 文件模型 LRU、租约与关闭
│   └── image_viewer.py         # 图像专用读取、归属核验与本轮游标
├── prompt/
│   ├── __init__.py             # 固定顺序装配稳定前缀
│   ├── role.py                 # 企业知识助手角色
│   ├── interaction.py          # 单工具、用户输出和系统反馈规则
│   ├── workspace.py            # 工作区定义与维护规则
│   └── guidance.py             # 程序生成反馈的统一格式
├── tool/
│   ├── __init__.py             # 工具公共入口
│   ├── registry.py             # 注册契约与能力组合
│   ├── workspace.py            # 三个工作区工具
│   ├── base/                   # 必须由业务模式继承实现的工具
│   │   ├── __init__.py
│   │   └── finish_task.py       # 最终输出契约，不提供默认实现
│   ├── context.py              # 主助手整理工具
│   ├── date_calculator.py      # 无状态日历日期运算
│   ├── sub_management_platform.py # 子管理平台五项信息与可折叠目录
│   ├── image_viewer.py         # 唯一图像工具 view_image_file
│   ├── external_expert.py      # 专家首次求助、追问、提示词与会话池
│   ├── web_search.py           # 搜索 HTTP、候选 LRU 与分页
│   ├── web_page.py             # 正文驻留、分页与打开工具
│   ├── progress.py             # 程序侧动作提示
│   └── result.py               # 统一执行状态、普通/折叠内容、提示与提交回调
├── context_budget/
│   ├── __init__.py
│   └── calculator.py           # 纯计数校验与预算计算
├── context_rendering/
│   ├── __init__.py
│   ├── block.py                # 自适应资料围栏与 YAML 元数据
│   ├── attachment.py           # 多图/音频/视频语义类型与 UUID 展示
│   ├── page.py                 # 通用页面定位、类型校验与轻量回执渲染
│   ├── history.py              # 历史任务原生消息与整理投影
│   ├── summary.py              # 树摘要正文展示
│   ├── task.py                 # 任务输入和结束记录
│   ├── workspace.py            # 中文区域、路径与 YAML 工作区
│   └── messages.py             # 独立消息顺序及当前输入装配
└── subgraph/
    ├── __init__.py
    └── context_management/
        ├── __init__.py
        ├── agent.py           # 有限整理循环
        ├── schema.py          # 工作区编辑工具集与 finish_compression
        ├── prompt.py          # 专用整理提示词
        ├── validation.py      # 来源、状态、引用和目标保护
        ├── workspace_compression/
        │   ├── __init__.py
        │   ├── runtime.py     # 多轮编辑、实际容量反馈和显式结束
        │   └── pending.py     # 预演保护、预留ID与原操作应用
        └── topic_summary/
            ├── __init__.py
            ├── schema.py      # 主题选择、两区域记忆与存储结构
            ├── prompt.py      # 主题发现与单主题总结提示词
            ├── rendering.py   # 选定来源及可读业务摘要
            └── workflow.py    # 增量规划、并行生成与完整验收
```

`subgraph` 沿用参考项目子能力组织方式，当前整理实现为异步子 Agent 函数，不表示新增了 LangGraph 状态图或后台任务。旧扁平 `prompt.py`、`tools.py` 和 `compression.py` 已删除，只有一套实际实现。

---

## 提示词分工

[主提示词入口](../../../../app/agent/core/prompt/__init__.py)按角色定位、交互规则、工作区定义三个部分装配，整理子 Agent 的专用提示词留在自身包中，不混入主前缀。

role 改为企业知识助手；interaction 与 workspace 以参考项目的通用文本和结构为基础。已对齐日期计算与任务时间头，固定北京时间展示并按各任务时间解释相对日期；页面展示规则由公共渲染器按实际资料注入；容量部分按本项目实际整理流程说明，不承诺不存在的百分比回执或其他工具。工作区的通用业务结构和三个编辑工具规则继续沿用，合同示例仅是结构示例，不限制角色服务范围。

运行时校验反馈复用 [guidance](../../../../app/agent/core/prompt/guidance.py) 构造器；用户资料中的相同标签不提升来源权限。版本、运行信息和内部节点标识不进入模型提示。这里只调整提示词职责和与实际能力一致的规则，不新增业务工具。

---

## 工具分工与依赖

工具按实现责任分为两类，共享的 registry、result、progress 属于注册与运行机制，不属于业务工具：

| 类别 | 位置与例子 | 使用方式 |
| --- | --- | --- |
| 可直接复用的具体实现 | `core/tool/` 下的工作区、上下文整理、时间、文件、网页、专家等能力模块 | 通过工厂注册；需要外部依赖的工具由宿主注入服务，不必按模式重写。 |
| 必须由模式继承实现的契约 | [base/finish_task.py](../../../../app/agent/core/tool/base/finish_task.py) | Core 只定义参数基类及注册结构；模式继承 FinishTaskTool，明确自己的说明、参数扩展及交付条件。 |

`FinishTaskTool` 是抽象类，不能直接实例化。`build_registry(finish_task=..., additional=...)` 必须显式接收该基类的具体实例。日期计算、当前时间、工作区和上下文整理由 `build_builtin_registrations()` 提供六个直接实现；随后加入模式最终输出，再加入可选工具。不能通过 additional 重名覆盖公共工具或最终输出。

探索使用 [ExplorationFinishTask](../../../../app/agent/exploration/tool/finish_task.py)，保持 content 正文与 SSE 交付；助理使用 [AssistantFinishTask](../../../../app/agent/assistant/tool/finish_task.py)，扩展必填 send_to_user，明确是否通知用户。新增模式必须自行实现，不能静默借用探索规则。

```python
from app.agent.core import AgentCore
from app.agent.core.tool import build_registry
from app.agent.exploration.tool.finish_task import ExplorationFinishTask

registry = build_registry(finish_task=ExplorationFinishTask())
core = AgentCore(settings, registry=registry)
```

直接构造 AgentCore 必须传入完整 ToolRegistry；构造、替换注册表以及执行入口均检查 finish_task 名称、final 输出类别与参数继承契约，缺失或不符合要求时在模型调用前失败。只用于独立执行某个工具或子图的 ToolRegistry 仍可没有最终输出，不强迫所有工具集合成为主循环。

每项注册统一绑定名称、说明、参数模型、预演处理器、可选兼容解析器、异步读取执行器、返回类型声明及页面解析器，并递归检查实际 Schema 字段说明。读取结果由 journal 成功提交后才进入有效轨迹，具体边界见[工具扩展](agent-modes.md#工具扩展与提交边界)。

注册表不按名称前缀猜测解析方式。工作区路径消歧解析器由工作区模块显式绑定；其他工具按自身参数模型处理。包入口导出的对象直接指向唯一实现，不保留另一套 `_MODELS` 或执行分派。

主循环使用 tool 和上下文管理子包的公共入口；树服务通过共用公开投影、模式上下文及 Core 的有效消息/固定预算入口装配输入；数据模型与私有持久化仍保持独立。有效提示词与工具集合在计数和生成间保持一致。导入包不读取 .env、不创建 HTTP 客户端、连接池或后台任务。

---

## 验证与范围

[包契约测试](../../../../tests/test_agent_core_packages.py)使用[工具与整理协议快照](../../../../tests/fixtures/agent_core_contract.json)，验证工具名称、顺序、Schema 和压缩协议。工具说明按[工具撰写规范](../../../standard/prompt-engineering.md#25-工具参数描述)受控更新快照；2026-10-22 风格调整已确认去除 description 后的 8 个工具定义完全一致，参数结构和约束没有变化。动态消息按[当前渲染契约](context-rendering.md)验证，不沿用旧的整条 user 消息哈希。主提示词按用户后续指示更新，不使用旧 system_prompt 哈希要求其保持不变；测试另验三组件顺序、已提供工具和未接入能力边界。

另覆盖每个公共导出的对象身份、按能力注册而非名称前缀分派、Schema 描述、反馈转义、模板插值以及全新解释器的无副作用导入。原有核心和隔离 MySQL/MyVector 回归继续运行。真实模型使用合成问题验证直接回答、工作区及用户输出、整理后继续，结果见[本轮实验](../../../../experiment/agent-core/20261021-package-prompts/analysis.md)。

本轮不新增 HTTP 路由，不修改实际配置、业务数据库、历史实验原始产物或其他项目。

2026-10-21 包重组阶段验证：完整 339 项通过，无跳过（含 53 项隔离 MySQL/MyVector），`pip check` 无冲突；该阶段相关文档 55 个相对文件链接有效。真实模型的三个合成样例全部通过，原始输入和输出保存在上述实验目录。后续模式装配验证记录在模式说明中，不以该历史数量代替最新结果。

公共返回类型、执行事实及自动折叠的单一契约见[工具内容说明](tool-content-contract.md)。


---

## 注册中心文件检索

`subgraph/file_retrieval/`包含schema、提示词、多轮agent、内部工具、排名和结果池，以及LangGraph装配。`tool/file_search.py`与`tool/file_candidates.py`提供主模型搜索和分页入口；源数据库读取、派生索引及混合召回位于repository/service。完整契约见[文件查询](file-retrieval.md)。


---

## 注册中心图像来源

`tool/registry_file.py`定义模型参数与注册，`file/registry_viewer.py`仅适配远程来源及身份，复用`ImageFileViewer`的页码、模型池和渲染流程；固定HTTP契约位于`infrastructure/registry_file.py`。参考[注册中心文件图像查看](registry-file-reading.md)。


2026-10-24 工具分类验证：[必需工具契约测试](../../../../tests/test_required_tools.py)新增 7 项，覆盖抽象类、显式装配、参数继承、错误输出类别、重名覆盖及模式交付差异。既有测试通过 [显式模式测试工厂](../../../../tests/agent_tool_helpers.py)装配探索最终输出，工具快照仅调整公共工具先于模式工具的注册顺序。完整 unittest 共 1218 项，1023 项通过、195 项因未配置隔离实库等环境跳过；本轮未启动 MySQL 容器，未进行真实消息交付。依赖、差异格式及本轮五份文档原有 103 个相对链接/锚点检查通过，两个新增测试链接亦存在。实际配置、历史实验与其他项目未修改。
