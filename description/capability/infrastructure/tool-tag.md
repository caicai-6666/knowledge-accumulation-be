# 可选工具格式注入

## 用途与边界

沿用 contract-service-be 的可选 tool-tag 方案，为模型提供与部署协议一致的工具调用格式说明。当前接入 [Agent Core](../../architecture/workflow/question-answering/agent-core.md)、助理与探索模式，以及历史/工作区压缩子 Agent 的初始提示词和纠错反馈。

实际工具定义仍通过 OpenAI `tools` 提交；vLLM 的 chat template 和 tool-call parser 负责将模型输出解析成原生调用。本项目只接受合法的原生 `tool_calls`，不从普通正文、XML 或 Markdown 中提取并执行调用。单工具、参数、权限及状态校验继续有效。

Embedding、无工具的普通生成和用户资料不自动注入。当前由内部 Python 装配使用；tool-tag 本身不更改服务端模板。初次引入时未请求真实模型。

---

## 配置与静态资产

在 [.env.example](../../../.env.example) 中提供以下配置，实际 `.env` 未修改：

```dotenv
VLLM_MLLM_TOOL_TAG_ENABLED=true
VLLM_MLLM_TOOL_TAG_FILE=qwen3.8-flash-next-nvfp4.txt
```

| 配置项 | 含义 |
| --- | --- |
| `TOOL_TAG_ENABLED` | 默认 true；false 时不读模板，不注入格式正文、标题或纠错格式说明，宿主显式模板也不能绕过关闭配置。 |
| `TOOL_TAG_FILE` | 默认上述 Qwen 文件；只接受无首尾空白的单个文件名，不接受绝对路径、子目录、反斜线或 NUL。关闭时仍保留协议标识，用于推理强度映射。 |

配置归属 [MLLMSettings](../../../app/config/mllm.py)，按项目既有 `.env` → 环境变量优先级加载。静态模板沿用源项目的协议格式，并按实际调用问题补充示例：

- [Qwen XML 格式](../../../assets/tool-tag/qwen3.8-flash-next-nvfp4.txt)。包含有参数与无参数两类示例；无参数时省略所有 parameter 标签，保留空的 function 块。
- [GLM XML 格式](../../../assets/tool-tag/glm-5.3-flash.txt)。
- [DeepSeek DSML 格式](../../../assets/tool-tag/deepseek-v4.1-flash.txt)。

与源项目的存放位置有所调整：本项目 `data/` 专用于真实运行数据，因此模板放在固定的 `assets/tool-tag/`，不受当前工作目录影响。部署产物须包含所选文件。文件名必须与模型、部署端模板和解析器相匹配，服务别名不会自动决定协议。自定义文件仍需部署侧保证兼容性。

---

## 加载与注入

[resolve_mllm_tool_tag](../../../app/infrastructure/tool_tag.py) 在创建 Agent 时读取 UTF-8 模板，拒绝符号链接、目录越界、文件缺失、不可读、编码错误和空白文件；错误不回退到另一协议。导入模块、加载配置、启动现有身份接口均不读取模板。

每个 Core 独立持有只读字符串快照，避免进程全局变量混用不同模型协议。修改配置或文件后须重新构造宿主，运行中的任务不重新读盘；`with_registry` 只替换工具集合并保留原快照，不重复注入。

主提示词按角色、交互、工作区组合，交互区域追加选定格式；压缩子 Agent 和两类纠错反馈共用该快照。关闭时仍保留通用原生工具规则、实际 `tools` 和业务约束，不清理用户原文或既有资料中自带的标签。

```python
from app.agent.exploration import build_core
from app.config.mllm import load_mllm_settings

core = build_core(load_mllm_settings())
# 首次装配完成加载，实际模型客户端仍按需创建。
```

通用 `AgentCore` 允许可信宿主传入 `tool_call_template`，用于独立装配和隔离测试，关闭开关仍优先。自定义 `system_prompt` 自动追加一次格式区域；需要自定义完整组合方式时使用 `prompt_builder(tool_call_template=...)`，不能同时传入两者。构建器应使用收到的模板，并遵循项目提示词规范；这些参数不接受用户原文作为系统规则。

模板文本不进行二次 `.format()`，保留 JSON 花括号和标签；系统纠错反馈按既有规则转义 XML 元字符。失败轨迹仅存在于连续纠错期间，动作成功后整段清除，完整私有生成仍保留。

---

## 分词与生成一致性

`MLLMSettings.thinking_template_kwargs(generation=...)` 统一提供思考参数；[生成客户端](../../../app/infrastructure/mllm.py)与[分词接口](../../../app/infrastructure/vllm_tokenizer.py)使用同一方法，按本次已校验的配置覆盖调用方的同名参数。

| 协议文件 | low / medium / xhigh |
| --- | --- |
| Qwen 默认及未知自定义文件 | low / medium / xhigh |
| GLM | low / high / max |
| DeepSeek | 50 / 75 / 100 |

此映射与参考项目一致，关闭 tool-tag 仍然有效；关闭思考时不传 `reasoning_effort`。主循环、任务前整理和工作区整理均计数实际消息、原生工具及相同生成配置，不以字符估算，也不另加未计入预算的格式文本。

---

## 隔离验证

[工具格式测试](../../../tests/test_tool_tag.py)覆盖文件与编码失败、路径/符号链接、导入无加载、关闭不读盘及屏蔽显式模板、快照与工具集合克隆、三协议主循环纠错清理、两模式装配、压缩纠错、并发模板隔离和普通 XML 文本不可执行。MockTransport 直接核对三协议各强度的生成/分词请求及关闭思考参数，不请求真实模型。

2026-10-21 验证：新增 16 项离线用例通过，完整回归 401 项通过，无跳过，其中 82 项为独立临时 MySQL/MyVector 验证；`pip check` 无冲突，12 份相关文档的 144 个相对文件链接有效。临时容器与卷已清理，实际 `.env`、业务数据及其他项目未修改。上述结果不代表真实模型的工具调用成功率或问答 HTTP/SSE 已验收。

2026-10-27 空参数调用排查：已补直接反馈与无参数格式示例；20项隔离回归通过，但当前实际模型合成验证仍失败，不能视为已修复。证据与边界见[诊断记录](../../../experiment/diagnostics/20261027-164445-qa-assistant-validation/analysis.md)。
