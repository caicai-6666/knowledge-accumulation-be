# Python 环境与依赖

## 用途与依赖职责

项目使用 Python 3.12，直接依赖统一维护在根目录的 [requirement.txt](../../../requirement.txt)，按已安装并验证的版本固定。

| 包 | 用途 | 使用入口 |
| --- | --- | --- |
| `fastapi` | HTTP API 框架。 | `from fastapi import FastAPI` |
| `openai` | 官方模型调用 SDK，包含异步客户端，无需单独安装异步包。 | `from openai import AsyncOpenAI` |
| `langgraph` | 智能体状态图与工作流编排。 | `from langgraph.graph import StateGraph` |
| `asyncmy` | 基于 asyncio 的 MySQL/MariaDB 驱动，支持异步连接与连接池。 | `import asyncmy` |
| `cryptography` | 为 asyncmy 的 `caching_sha2_password` / `sha256_password` 完整 RSA 认证提供加密实现。 | 由驱动认证模块调用。 |
| `httpx` | 中间件异步 HTTP 客户端。 | `httpx.AsyncClient` |
| `websockets==16.1.1` | 钉钉机器人 Stream 长连接；与异步 HTTP 适配配合，不跟随重定向或环境代理。 | `websockets.asyncio.client.connect` |
| `pydantic` | 接入配置及响应契约校验。 | `BaseModel`、`SecretStr` |
| `PyYAML` | Core 工作区及任务元数据的可读 YAML 展示。 | `yaml.SafeDumper`、`safe_dump` |
| `python-dotenv` | 加载项目根目录 `.env`。 | `dotenv_values` |
| `uvicorn` | 启动 FastAPI ASGI 服务。 | `python -m uvicorn` |
| `Pillow` | 头像内容校验和无损 WebP 编码。 | `from PIL import Image` |
| `pillow-heif==1.8.1` | 机器人 HEIC/HEIF 图片解码，接入现有 Pillow 到 PDF 转换流程。 | `register_heif_opener` |
| `PyMuPDF==1.28.2` | Core 图像附件的真实格式检查、按页 PNG 渲染及 LRU 文档模型。 | `import pymupdf` |
| `llama-index-core` | MyVector 向量库协议、节点及异步检索。 | `VectorStoreIndex`、`BasePydanticVectorStore` |
| `llama-index-embeddings-openai-like` | 沿用 registry 验证的 vLLM 自定义模型异步 Embedding 适配。 | `OpenAILikeEmbedding` |

包信息参考：[FastAPI](https://pypi.org/project/fastapi/)、[OpenAI SDK](https://pypi.org/project/openai/)、[LangGraph](https://pypi.org/project/langgraph/)、[asyncmy](https://pypi.org/project/asyncmy/)。

---

## 安装与检查

首次安装时创建 Python 3.12 环境：

```bash
conda create -n knowledge-accumulation-be python=3.12 pip -y
```

本机 Conda 安装在 `/Users/caicai/miniconda3`，项目环境名为 `knowledge-accumulation-be`。若当前终端提示找不到 `conda`，先执行 `source /Users/caicai/miniconda3/etc/profile.d/conda.sh`，再在项目根目录执行：

```bash
conda activate knowledge-accumulation-be
python --version
python -m pip install -r requirement.txt
python -m pip check
```

其他机器需先准备 Python 3.12 的隔离环境，再执行安装命令。使用 `python -m pip`，确保依赖安装到当前解释器环境。

---

## MySQL 8 认证依赖

`asyncmy==0.2.16` 的已安装包元数据未声明 RSA extra 或自动安装 `cryptography`。驱动在 `sha256_password` 和 `caching_sha2_password` 需要 RSA 的完整认证路径中使用该包，缺失时即使网络和账号正确，也会在执行 DDL 或业务 SQL 前抛出缺失依赖的 RuntimeError。Mock 连接池无法验证这条路径，已有认证缓存的快速路径也不能替代完整认证验证。

因此在 `requirement.txt` 显式固定 `cryptography==50.0.2`，已安装至本项目 Conda 环境。无需更换数据库认证插件或绕过驱动错误；服务端仍保持原有认证配置。若某个解释器进程在安装前已经导入 asyncmy，它可能保留依赖不可用的模块状态，补验应使用新解释器进程，不需要重启共享服务。

[RSA 认证依赖测试](../../../tests/test_mysql_auth.py)在内存生成临时密钥，实际执行驱动 `sha2_rsa_encrypt` 并解密验证协议载荷，不 Mock 加密函数、不连接数据库、不写密钥文件。此前 RSA 修复阶段完整 87 项测试及 `pip check` 通过，该历史验证保留。最终职责下注册/登录回归见[当前验证记录](../../architecture/system/user-auth.md#验证与切换前提)。协作方随后报告独立临时 MySQL 8.4 中的默认 SHA-2 认证握手及人员仓库补验通过，来源与范围见[隔离实库补验](../infrastructure/user-infrastructure.md#协作方隔离-mysql-84-实库补验)。本地 RSA 单测本身不替代实库验证，两者均不代表生产联通。

---

## HEIC / HEIF 解码

机器人入站使用 `pillow-heif==1.8.1` 为 Pillow 注册 HEIF 解码器，复用已有 PDF 转换；具体格式和边界见[机器人入站](assistant-inbound.md#文件与队列)。按[官方安装说明](https://pillow-heif.readthedocs.io/en/stable/installation.html)优先使用预编译 wheel，不依赖系统外部转换命令；无适用 wheel 的平台需自行准备 libheif 等构建依赖。Dockerfile 仍通过 `requirement.txt` 安装，已有后端镜像需重建。

2026-10-26 已在本机 Python 3.12 / macOS arm64 Conda 环境安装预编译包，并通过真实 HEVC 编解码、相关 17 项测试及 `pip check`。本次未重建 Linux 后端镜像，未验证真实钉钉或 iPhone 原片。

---

## 当前边界

FastAPI 应用及中间件接入已实现，启动方式见[中间件平台注册与心跳](../infrastructure/middleware.md)。人员仓库、钉钉和头像适配见[用户基础设施](../infrastructure/user-infrastructure.md)，启动自动同步用户及管理员总表。问答树及记忆已提供存储仓库，另有 [MyVector/vLLM 适配](../infrastructure/myvector.md)；内部 Core 已支持[图像阅读](../../architecture/workflow/question-answering/image-reading.md)，已接入探索问答 HTTP/SSE，配置 Embedding 地址后启动[记忆自动向量化](memory-embedding.md)，后台扫描已落盘消息并请求向量模型；主模型仍按问答需求调用。

本地依赖检查覆盖包兼容性、模块导入、异步客户端初始化与关闭、最小状态图及 RSA 分支，不发送模型请求或连接数据库。协作方隔离 MySQL 8.4 补验通过；真实模型样例另见[Core 实验](../../../experiment/agent-core/20261021-package-prompts/analysis.md)，不代表生产钉钉或共享业务服务已验收。测试和联调必须遵守[项目开发规范](../../../AGENTS.md)的数据目录边界。

探索问答 multipart PDF 上传使用 `python-multipart==0.0.32`，已在 `requirement.txt` 声明；SSE 使用 FastAPI/Starlette 自带 `StreamingResponse`，不额外引入 SSE 包。

网页正文提取新增 `trafilatura==2.3.1` 和直接依赖 `lxml==6.1.3`，已安装于上述 Conda 环境；HTTP、模型精炼与 LangGraph 复用现有依赖，使用方式见[网页搜索与正文读取](../../architecture/workflow/question-answering/web-search.md)。
