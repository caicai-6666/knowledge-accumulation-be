# 文件内容告知检查

本文档说明个人助理在向其他用户转述平台文件资料前的主动权限核对工具 `check_file_disclosure`。

---

## 1. 用途与边界

当个人助理准备把某份平台文件或注册中心文件的具体内容转述给另一位员工（或通过 `send_assistant_message` 通知对方助理）时，可调用 `check_file_disclosure` 核对目标员工是否具备相应平台访问权限。

> **设计边界：** 该工具用于辅助模型主动核对访问权限并提供明确反馈，不包含自动来源追踪或对最终正文的强制内容门禁。

---

## 2. 实现要点

- 源码位置：`app/agent/assistant/tool/file_disclosure.py`，已迁移至 `agent-core` `RegisteredTool` 契约并由 `build_assistant_tools` 装配。
- 所有参数（含嵌套字段）均具备非空中文 `description`，通过 `agent_core.core.tool.registry.check_descriptions` 校验。
- 末尾挂接 `FeedbackOnlyHook`，将结构化核对结果渲染为面向模型的中文 Markdown 结论（包含允许/拒绝原因及下一步建议），不输出原始 JSON。
