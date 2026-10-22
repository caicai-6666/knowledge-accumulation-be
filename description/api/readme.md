# API 接口导航

新后端业务接口统一使用 `/knowledge-accumulation/api`，当前包含用户登录、中央会话身份查询、头像及任务附件读取、平台信息查询及探索问答树创建、列表、改名、打开、删除及任务注册/SSE/快照/取消。registry 的 `/knowledge-accumulation/registry/api` 保持独立。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [显式登录](auth.md#显式登录) | `POST` | `/knowledge-accumulation/api/auth/login` |
| [查询当前身份](auth.md#查询当前身份) | `GET` | `/knowledge-accumulation/api/auth/me` |
| [读取头像](resource.md#读取头像) | `GET` | `/knowledge-accumulation/api/resource/avatar/{filename}` |
| [读取任务附件](resource.md#读取任务附件) | `GET` | `/knowledge-accumulation/api/resource/attachment/{tree_id}/{file_id}` |
| [查询平台列表](platform.md#查询平台列表) | `GET` | `/knowledge-accumulation/api/platform/list` |
| [探索问答树列表](exploration.md#获取问答树列表) | `GET` | `/knowledge-accumulation/api/exploration/trees` |
| [创建探索问答树](exploration.md#创建问答树) | `POST` | `/knowledge-accumulation/api/exploration/trees` |
| [修改探索问答树名称](exploration.md#修改问答树名称) | `PATCH` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/name` |
| [打开探索问答树](exploration.md#打开问答树) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/open` |
| [删除探索问答树](exploration.md#删除问答树) | `DELETE` | `/knowledge-accumulation/api/exploration/trees/{tree_id}` |
| [注册探索任务](exploration.md#注册探索任务) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns` |
| [订阅并激活探索任务](exploration.md#订阅并激活探索任务) | `GET` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/events` |
| [获取探索任务快照](exploration.md#获取探索任务快照) | `GET` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}` |
| [停止探索任务](exploration.md#停止探索任务) | `POST` | `/knowledge-accumulation/api/exploration/trees/{tree_id}/turns/{turn_id}/cancel` |

接口字段、错误及调用示例见[用户认证 API](auth.md)，运行配置与会话设计见[登录与会话管理](../architecture/system/user-auth.md)。
