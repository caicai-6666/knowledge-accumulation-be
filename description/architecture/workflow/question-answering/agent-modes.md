# 助理与探索模式装配

## 用途与边界

个人助理是复杂任务探索的简化使用方式，两者共用 Agent Core、工作区、预算、压缩、问答树和节点持久化。业务差异放在模式包内，Core 不判断 `assistant` / `exploration`。

当前已实现模式提示词、工具组合、上下文资料契约与内部服务工厂；默认仍只有六个[基本工具](agent-core.md#必要工具)。个人资料、日程、记忆检索等具体业务工具及其真实数据读取尚未实现，不建立虚构的工具或数据源。没有新增 HTTP、SSE 或数据库迁移。

---

## 包结构与职责

```text
app/agent/
├── core/                        # 通用循环、基本工具、预算、整理、提交
├── context.py                   # 树公开路径到 ContextInjection 的共用投影
├── assistant/
│   ├── __init__.py               # 显式导出工厂及资料契约
│   ├── factory.py                # 助理 Core 与持久化服务装配
│   ├── context.py                # 个人背景、偏好、相关记忆的本轮注入
│   ├── prompt/
│   │   ├── __init__.py           # 组合通用交互、工作区和助理角色
│   │   └── role.py               # 个人工作辅助规则
│   └── tool/
│       └── __init__.py           # 基本工具与助理工具组合入口
└── exploration/
    ├── __init__.py
    ├── factory.py                # 探索 Core 与持久化服务装配
    ├── context.py                # 探索上下文构建入口
    ├── prompt/
    │   ├── __init__.py
    │   └── role.py               # 多方向分析与证据核实规则
    └── tool/
        └── __init__.py           # 基本工具与探索工具组合入口
```

未来实际实现资料或记忆工具时，在对应 `tool/` 下按业务能力增加模块，由 `build_registrations()` 导出；当前该方法返回空元组。跨模式复用的业务能力确定后再抽取，数据库和外部服务访问仍放在现有 repository / infrastructure / service 中，模型工具只是适配入口。

导入上述包不加载 `.env`，不创建 HTTP 客户端、数据库连接或后台任务。模式工厂在未传 options 时读取[文件与页面运行配置](image-reading.md#运行配置)，构造 Core 不立即打开连接；如启用 [tool-tag](../../../capability/infrastructure/tool-tag.md)，仅此时加载所选静态格式文件，两模式复用同一加载规则。外部传入的模型客户端由宿主管理，自建客户端沿用 Core 的关闭规则。

---

## 工厂与调用

两个模式均导出：

| 接口 | 行为 |
| --- | --- |
| `build_core(settings, client=..., counter=..., options=..., tools=...)` | 返回配置了该模式角色及工具集合的通用 Core，适合内存宿主。 |
| `build_service(nodes, settings, ..., tools=..., tool_loader=...)` | 返回复用现有节点仓库的 QuestionAgentService，绑定对应树用途。 |

助理 `build_service` 另接收 `context_loader`。两类 loader 均接受本次可信 `AuthenticatedUser` 的深拷贝，支持同步返回或异步返回：

- `tool_loader(user)` 返回已经按当前身份和权限筛选的 RegisteredTool 序列。回调中可将可信身份绑定到执行器闭包，不接受模型传来的 userid 作为授权依据。
- `context_loader(user)` 返回 AssistantContext 或符合该契约的字典。每次运行重新加载，不跨用户缓存个人背景。

`tools` 是宿主提供的固定注册项；只适合不绑定具体用户身份的工具。身份相关工具使用 `tool_loader`，服务为本轮创建独立工具集合，不修改共享 Core。重复工具名（包含与基本工具重名）明确拒绝，不能覆盖基本交互协议。实际业务读取仍需执行其授权校验，工具是否可见不代替资源鉴权。

```python
from app.agent.assistant import build_service

# nodes、settings、client 由现有宿主提供；两个 loader 为可信业务适配器。
service = build_service(
    nodes, settings, client=client,
    context_loader=load_personal_context,
    tool_loader=load_authorized_tools,
)
result = await service.generate(
    user=authenticated_user, tree_id=assistant_tree_id,
    question="请结合我提供的背景整理下一步工作。",
    context_injection="本次补充资料。", thinking_effort="medium",
)
```

助理不接收节点选择，自动从主干末端继续；探索 `build_service` 对非空树要求 `selected_node_id`。树用途不匹配时，在调用 loader、模型和创建任务前失败。旧的直接 `AgentCore(...)` 及 `QuestionAgentService(nodes, core)` 仍保留通用角色和原有调用方式。

---

## 上下文与提示词

共用 `build_tree_context` 只投影当前父链的摘要、公开问答、中途输出及对应工作区，不加载其他分支或原生推理。模式构建器在该结果上附加资料，不另造树加载机制。

AssistantContext 分为 `profile`、`preferences`、`relevant_memories`；每个 ContextMaterial 包含 `content`、`source`、`status`，默认 `unverified`。它们不包含权限覆盖字段，用户陈述不能自动变成已确认事实。

个人资料与内部调用方的当前资料通过 `ContextInjection.supplemental` 保存，主循环在“上下文资料”文本分区作为普通 user 数据展示，保留来源和确认状态，不提升为 system/tool 指令；不自动登记记忆，也不自动修改权威工作区。空个人资料保持原始输入不变。任务前摘要完成后仍保留本轮个人资料，不因重建 ContextInjection 丢失。该内部扩展点不是探索 HTTP 的额外用户输入字段。

模式角色复用通用知识助手的事实、权限与服务边界，再加入个人辅助或复杂任务分析规则。通用交互与四区域工作区定义继续只维护一份。资料缺失、冲突或工具不可用时明确限制，不承诺未接入能力；角色文本不含树类型、内部节点、工作流拓扑或运行标识。

Core 只接收有效 `system_prompt` 与 ToolRegistry。任务前计数、固定预算、主循环消息与生成共用 `core.build_messages` / `core.fixed_messages` 及同一工具集合；本轮按用户装配的局部 Core 保持模型配置和运行选项一致。整理子 Agent 仍使用自己的提示词及 finish_compression，不能调用模式业务工具。

---

## 工具扩展与提交边界

RegisteredTool 仍绑定名称、描述、参数模型、纯预演 handler 和可选 parser，新增可选 `executor(arguments)`。handler 只在工作区副本上预演，不访问外部系统或修改共享状态；executor 在参数、状态和候选工作区验收后读取资料，支持异步返回。

所有实际参数 Schema 的属性说明继续递归校验。执行器统一返回 `ToolExecutionResult`，不再接受任意 JSON 作为成功结果；`ToolExecution` 只用于携带提交后的本地回调。注册必须声明返回内容类别，可折叠工具还需绑定页面解析器，完整定义见[公共工具契约](tool-content-contract.md)。普通读取可返回：

```python
ToolExecutionResult(status='succeeded', tool_result={
    'status': 'accepted', 'result': {'text': '资料内容', 'source': '业务来源'},
})
```

结果随 assistant/tool 对及工作区成功提交后进入下一轮上下文。`failed` 反馈只存在于连续纠错轨迹；成功后整段清除。`unknown`、异常、取消、非法结果或提交失败停止，不伪造成功或自动重放。原始生成完整保存在私有记录中。

当前 executor 入口用于资料读取。外部写操作与本地节点事务不具备原子保证，本轮不接入此类动作；后续若需要维护外部业务状态，应先明确幂等与提交结果不确定时的处理契约。

---

## 验证与变更入口

[离线模式测试](../../../../tests/test_agent_modes.py)验证模式与工具集合隔离、资料来源与状态、参数验收、异步执行结果、纠错清理、工作区预演隔离、取消与故障、提交失败不暴露结果以及提示词/工具计数一致性。[独立 MySQL 测试](../../../../tests/test_agent_modes_mysql.py)验证真实落盘、助理压缩后资料保留、探索分支恢复、用途不匹配、跨用户资料及并发工具身份隔离。测试模型和业务服务均为替身，数据库仅使用一次性临时容器，不访问实际配置或共享业务服务。

本次新增文件为上述模式包（共 12 个文件）、共用 `app/agent/context.py`、两份测试和本说明；修改 Core 的提示词组合、消息渲染、工具注册与运行循环，以及 QuestionAgentService、包导入测试和文档导航。未增加实际的 profile/memory/calendar 工具文件。

2026-10-21 验证：完整 367 项通过，无跳过，包含 69 项独立临时 MySQL/MyVector 验证；本次新增 8 项离线与 8 项模式实库测试。`pip check` 无依赖冲突，本次维护的 6 份文档共 59 个相对文件链接有效。临时容器与临时卷在验证后清理，未修改真实 `.env`、业务数据库或其他项目，未执行真实业务服务或模型联调。
