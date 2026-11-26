# 双源 PDF 逐页查看

## 用途与范围

探索模式与个人助理统一使用单个 `view_pdf` 工具按页查看 PDF 原件图像，替代原有的双工具拆分，自动路由两类互不重叠的文件来源：

- **源 A（本地上传附件）**：36 位小写带连字符 UUID，经 `authorize_local_file` 校验属于当前用户与当前树后，从 `UploadFileStore`（语义类型 `images`，`.pdf` 文件）读取字节。
- **源 B（注册中心远程 PDF）**：`1` 至 `18446744073709551615` 的十进制正整数字符串，经 `RegistryFileAccessService` 按可信 `user_id` 校验 SQL 权威权限后，由 `RegistryFileClient` 携带平台会话令牌与可信 `userid` 下载 PDF 原件字节。

相关代码：

- 探索双源查看器：`app/agent/exploration/tools/pdf_viewer.py`（`create_exploration_pdf_viewer`、`build_exploration_pdf_tool`）
- 助理公开门禁包装：`app/agent/assistant/tool/image_authorization.py`（`wrap_image_authorize`）
- 远程鉴权与客户端：`app/service/registry_file_access.py`、`app/infrastructure/registry_file.py`
- 本地文件存储：`app/infrastructure/upload_file.py`

---

## 核心实现与鉴权策略

1. **底层复用 `agent-core` `PDFViewer`**：基于 `agent_core.PDFViewer`、`create_pdf_cache_pool(scope='session')` 与 `build_pdf_view_registration` 构建，按需渲染 PNG 图像并通过 `FoldableToolContent` 展示，末尾由 `FeedbackOnlyHook` 渲染可读回执。
2. **每次查看与页面解析均重新鉴权**：即使命中 `CachePool`，`PDFViewer.view` 与 `resolve_page` 仍先执行 `authorize(file_id)`，防止撤权或删树后继续读取缓存页面。
3. **动态进度文案**：通过 `file_name(file_id)` 返回已授权记录的文件名（注册中心取入库 `name`，本地附件取登记文件名），供 `progress_factory` 展示「正在查看 PDF 文件：xxx」。
4. **个人助理 `public` 可见性门禁**：助理装配 `view_pdf` 时用 `wrap_image_authorize` 包装本地附件授权回调，在树归属校验之后追加校验 `uploaded_files.visibility == 'public'`；私密附件抛出 `PDFViewError('attachment_not_public', ...)`，明确告知限制且不自动公开文件。

---

## 错误分层

| 场景 | `failure_mode` | 处理语义 |
| --- | --- | --- |
| 文件不存在、无权访问、非 PDF 格式、越界页码、助理私密附件拦截 | `correctable` | 返回定向修正提示，阅读游标不推进。 |
| 注册中心权限校验或下载服务暂不可用、可见性查询故障 | `failed` | 保存失败事实并告知服务暂不可用，不推断文件不存在。 |
| 查看会话已关闭 | `dependency` | 停止当前调用，需在有效任务中重新查看。 |

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_tools.py`（双源路由、本地/远程鉴权拒绝与 PNG 渲染）与 `tests/assistant_v2/test_assistant_tools.py`（助理私密拦截与公开放行）全部通过。
- 端到端测试：`tests/exploration_v2/test_exploration_e2e.py` 验证上传附件转 PDF 后通过 `view_pdf` 成功读取。
