# 工具失败处理

主助理、探索模式共用 Core 分层执行；内部检索和正文转换仍有自己的有限生成预算。目标是让模型诚实解释已确认失败，同时保留参数纠错与写入保护。权威契约见[反馈规范](../../../standard/tool-feedback.md#失败分层与主循环)。

---

## 适配范围

| 文件 / 模块（相对 app/agent） | 本次处理 |
| --- | --- |
| `core/model.py`、`core/runtime.py` | 宿主依赖异常；失败事实提交与纠错分离；审计失败不可吞掉；unknown 与 dependency 分别停止。 |
| `core/tool/result.py`、`registry.py`、`hooks.py` | failure_mode、业务钩子明确失败信封、禁止渲染篡改状态或分类。 |
| `core/tool/file_search.py`、`memory_search.py` | 内部生成/检索耗尽作为执行失败；不发布候选；关闭与审计故障停止。 |
| `core/subgraph/file_retrieval/tool.py`、`core/subgraph/memory_retrieval/agent.py`、`session.py` | 权限与宿主异常透传；模型请求失败不伪装空结果或确定宕机。内部参数纠错仍有上限。 |
| `core/tool/file_candidates.py`、`memory_viewer.py`、`image_viewer.py`、`registry_file.py`、`core/file/image_viewer.py` | 读取失败可继续，非法页码/引用可纠正；失败不推进游标，关闭停止。注册中心阅读复用图像查看器，宿主依赖异常不转换为单文件不可用。 |
| `core/tool/external_expert.py` | 请求失败、截断或内部轮数/容量耗尽为执行失败；缺失临时会话可纠正，池关闭停止，私有审计失败透传。 |
| `core/tool/web_search.py`、`web_page.py`、`sub_management_platform.py` | 搜索/读取失败与空结果分离；宿主授权或生命周期失效停止。 |
| `core/tool/platform_a2a.py` | 明确失败或等待失败可继续；发布未知仍停止，不自动重发。 |
| `assistant/tool/search_assistants.py`、`portrait_query.py`、`search_calendar_events.py`、`view_calendar_query.py`、`memory_window.py` | 目录、画像、日程、记忆窗口读取失败不累计参数错误；非法范围/ID仍可纠正。 |
| `assistant/tool/image_authorization.py`、`app/service/question_agent.py`（后者相对项目根） | 公开状态包装透传宿主异常；实际上传鉴权把树归属失效与单文件不存在分开，前者停止、后者可纠正。 |
| `assistant/tool/file_note.py`、`file_note_query.py`、`file_visibility.py` | 明确未写入的前置检查失败/读取失败可继续；写入结果未知保留停止。 |
| `assistant/tool/finish_task.py`、`assistant/message_rendering/agent.py`、`hook.py` | 附件前置检查/正文转换明确失败可继续；未提交和未发送必须明示；转换审计失败、关闭、取消停止。 |

日期、当前时间、工作区、上下文操作、提醒、画像增删、助理消息发送等工具的 Schema 与业务前置拒绝保持 correctable；依赖写入和权威提交异常保持停止。没有新增后台重放或改变幂等策略。只读结果过期与宿主撤权区分处理，不因无法读取单个对象推断用户失去整体身份。

---

## finish_task 转换边界

转换失败时，主模型下一轮收到“正文及附件均未登记发送”及原因，可重新组织完整正文。失败的 finish_task 不结束任务、不保存候选答案、不调用交付回调。后续转换成功后才正常提交最终正文并交付一次。实际发送后返回错误仍按原交付恢复协议处理，不通过此失败分支自动重复发送。

---

## 验证与限制

`tests/test_tool_failure_layers.py` 覆盖两种模式、连续执行失败、真实记忆子智能体连续 3 次纠错耗尽后外层连续调用 4 次仍可结束、三次参数错误停止、计数重置与纠错清理、unknown/dependency/取消/存储失败停止、成功提交回调保护、渲染不可改分类、真实 finish_task 注册项转换失败后重写且只交付一次、专家临时会话与宿主关闭的区别。

真实模型实验使用合成工具失败与空结果，没有读取业务数据库或发送消息；两种主模式各验证专家、附件、历史查询失败及完整空集。原始输入、请求、响应、审计、答案与人工分析位于[实验记录](../../../../experiment/tool-failure-honesty/20261113-live-boundaries/analysis.md)。不能据此声称所有自然语言任务或真实外部故障都已覆盖。


2026-11-13 验证：194 项回归中 172 项通过、22 项隔离数据库测试未启用。none 推理合成回执实验 8/8 完成；[生产专家注册项默认推理实验](../../../../experiment/tool-failure-honesty/20261113-production-expert-default/analysis.md)中，个人助理两次请求失败后第三轮诚实结束，探索在第一轮主模型阶段达到 240 秒实验时限，未执行专家工具，不能声称该模式默认配置下的在线恢复已验证。
