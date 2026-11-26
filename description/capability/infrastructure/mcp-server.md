# MCP Server 接入与通用工具封装

## 用途与范围

本项目通过 `MCPClientManager` 接入外部 `knowledge-accumulation-mcp-server`（Streamable HTTP 协议），复用其托管的 7 个通用智能体工具及文件转 PDF 能力（`file_conversion`），供探索模式、个人助理与钉钉机器人入站共用。

- 客户端与配置：`app/infrastructure/mcp_client.py`、`app/config/mcp.py`
- 工具二次封装：`app/agent/exploration/mcp_tools.py`
- 附件转换调用：`app/service/exploration_attachment.py`、`app/infrastructure/robot_attachment.py`

---

## 接入配置与客户端生命周期

环境变量由 `load_mcp_settings()`（`app/config/mcp.py`）读取：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_ENABLED` | `true` | 是否启用 MCP 客户端；关闭时不建立连接、不装配 MCP 工具。 |
| `MCP_SERVER_URL` | `http://127.0.0.1:18100/mcp` | MCP Server Streamable HTTP 端点地址。 |
| `MCP_TIMEOUT_SECONDS` | `360.0` | 单次工具调用与文件转换超时时间（秒）。 |

`MCPClientManager` 在应用启动阶段执行 `await client.start()` 建立长驻 `httpx.AsyncClient` 与 `streamable_http_client` / `ClientSession`，应用退出时 `await client.aclose()` 释放连接。

- `list_tools(refresh=False)`：拉取并缓存远端工具清单，供探索模式与个人助理共享装配。
- `call_tool(name, arguments)`：执行远端工具调用；遇到传输层异常或连接断开时自动重连并重试一次，超时或服务不可达抛出 `MCPUnavailableError`。
- `convert_file(content_bytes, filename=None, *, operation='auto') -> ConvertedPDF`：调用 MCP `file_conversion` 工具将上传的图片或 Office 文档转为 PDF 字节流，错误结构化为 `ConversionError(code, reason)`。

---

## 7 个通用工具的二次封装

`build_mcp_exploration_tools(client)`（`app/agent/exploration/mcp_tools.py`）将 MCP Server 暴露的 7 个工具转换为 `agent-core` 的 `RegisteredTool`，并统一补齐协议、进度与渲染契约：

| 工具名 | 职责 | 特殊展示与校验 |
| --- | --- | --- |
| `get_current_time` | 获取当前北京时间 | 无参对象放宽 `extra='ignore'` 兼容模型占位键，实际向 MCP 固定传 `{}`。 |
| `calculate_date` | 日期推算与间隔计算 | 有参严格 `extra='forbid'`。 |
| `search_web` | 发起公网搜索并建立结果集 | 进度文案映射为 `online-search`。 |
| `view_web_search_results` | 分页查看搜索结果摘要 | 返回可折叠文本资料页（`FoldableToolContent`）。 |
| `open_web_page` | 读取网页正文与配图 | 有内容时拆分为可折叠文本/图像页面；无内容时走普通回执分支（`no_content`）。 |
| `ask_external_expert` | 向外部推理专家发起新会话 | 进度类型为 `external-expert`，返回会话 ID。 |
| `continue_external_expert` | 接续已有外部专家会话 | 进度类型为 `external-expert`。 |

封装规则：

1. **动态参数模型与字段校验**：将 MCP JSON Schema 递归转换为 Pydantic `BaseModel`，保留所有层级字段的非空 `description`，并经 `agent_core.core.tool.registry.check_descriptions` 校验。
2. **动态进度映射**：解析 MCP `meta.progress` 映射为合规 `ToolProgress`（白名单类型与「正在…」前缀）。
3. **错误分层**：按 MCP 业务错误码精确映射为 `agent-core` 的 `correctable`（参数/会话可修正）、`failed`（查询或远端执行未完成）与 `dependency`（MCP 连接或依赖不可用）。
4. **末尾纯展示钩子**：每个工具末尾均挂载 `FeedbackOnlyHook`，将结构化结果渲染为清晰中文 Markdown，禁止直接暴露原始 JSON。

---

## 部署注意事项

> **部署待办（跨仓库）：** `knowledge-accumulation-mcp-server` 启用了 Host 校验时，容器化部署需在其允许的 Host 列表中放行后端容器访问时使用的主机名（例如 `host.docker.internal` 或 Docker Compose 服务名），否则 Streamable HTTP 握手会返回 421/403。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_mcp_tools.py`（含动态 Schema 校验、无参占位键兼容、网页图文可折叠与 `no_content`、错误分层与 `FeedbackOnlyHook` 渲染）及 `tests/assistant_v2/test_robot_attachment.py` 全部通过。
- 真实容器联调：已在独立测试容器中验证 `get_current_time`、`calculate_date`、`search_web`、`view_web_search_results` 与真实 MCP Server 通信走通。
