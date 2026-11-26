# 子管理平台目录

## 用途与范围

`list_sub_management_platforms` 为探索模式与个人助理提供实时只读的子管理平台目录查看能力，返回中间件登记的子管理平台名称、平台代码、功能说明、A2A 职责与存活状态（包含离线平台），供文件检索平台筛选及跨平台 A2A 协作选型。

- 工具实现：`app/agent/exploration/tools/platform_tools.py`（`SubManagementPlatformViewer`、`build_sub_management_platform_tool`）
- 目录读取服务：`app/service/platform_directory.py`（`PlatformDirectoryService`）
- 数据契约：`app/model/platform.py`（`MiddlewarePlatformInfo`）

---

## 核心实现与边界

- **无参容错**：`ListSubManagementPlatformsArguments` 在前置校验中丢弃未使用的对象字段，始终读取最新完整目录。
- **可折叠展示**：存在子管理平台时，返回 `FoldableToolContent` 文本页面；`SubManagementPlatformViewer` 仅在 `after_commit` 后保留最新一次目录页面，避免重复查询在上下文中累积长正文。
- **过滤边界**：仅依据中间件返回的 `is_sub_management_platform` 标记筛选，不从用户访问等级、管理员关系或在线状态推断。目录不代表文件访问或 A2A 授权。
- **末尾渲染钩子**：挂载 `FeedbackOnlyHook(render_directory_feedback)`，对成功（含 `visible`/`hidden`/`unavailable` 三态回执）、空目录（`count == 0`）与依赖故障（`failure_mode='failed'`）均输出可读中文 Markdown。

---

## 验证状态

- 单元测试：`tests/exploration_v2/test_tools.py` 覆盖成功可折叠页面提交前后可见性、空目录、服务故障与 `ToolRegistry` Schema 校验，全部通过。
