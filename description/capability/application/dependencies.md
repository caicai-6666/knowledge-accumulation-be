# Python 环境与依赖

本文档说明 `knowledge-accumulation-be` 的 Python 运行环境、核心依赖及外部组件要求。

---

## 1. 运行环境

- **Python 版本**：Python 3.12（本地开发环境 conda `knowledge-accumulation-be`；容器基础镜像 `python:3.12-slim`）。
- **依赖清单**：见仓库根目录 `requirement.txt`。

---

## 2. 核心依赖分层

| 类别 | 主要依赖 | 用途 |
| --- | --- | --- |
| Agent 核心库 | `agent-core[mysql,pdf]`（Git 安装） | 提供 `AgentRuntime`、`TreeAgent`、`MySQLStorage` / `SQLiteStorage`、内置工具、上下文压缩、向量/全文记忆及 1B 提醒/心跳调度。 |
| MCP 客户端 | `mcp>=2.3.0` | 通过官方 Streamable HTTP 协议对接外部 `knowledge-accumulation-mcp-server`（7 个外部探索工具与 `file_conversion`）。 |
| Web 与异步 IO | `fastapi`、`uvicorn`、`httpx`、`pydantic`、`python-dotenv` | HTTP/SSE 接口、配置校验与异步客户端。 |
| 模型客户端 | `openai==2.54.0`、`llama-index-embeddings-openai-like` | 对接 vLLM OpenAI 兼容 MLLM 与 Embedding 接口（版本与 `agent-core` `openai>=1.70,<3` 约束一致）。 |
| 存储与文档 | `asyncmy`、`pymupdf`、`Pillow` | MySQL 异步连接池、PDF 合法性校验/页面渲染与头像 WebP 转码。 |
| 钉钉接入 | `dingtalk-stream` | 钉钉单聊机器人 Stream 入站长连接。 |

---

## 3. 已移除的历史依赖

完成向 `agent-core` 与外部 MCP 服务迁移后，以下旧依赖与本地能力已不再使用：

- 本地 LibreOffice（`soffice`）及 `app/infrastructure/file_conversion/`（已迁移至外部 MCP `file_conversion`）；
- 本地 `MyVector` 向量缓存、旧版 `app/agent/core/` 手动循环及独立 DeepSeek 客户端。
