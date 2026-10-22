# 人员与权限数据契约

## 适用范围与来源

本契约用于新后端的用户基础设施及全表注册。沿用来源项目的人员字段语义，不迁入注册中心的资源、审核表或工作流。仓库和运行配置见[用户基础设施](../../capability/infrastructure/user-infrastructure.md)，注册发布规则见[用户全表注册协调](../../capability/application/startup-user-sync.md)。

表结构以部署项目的 [003-personnel.sql](../../../../registry-deploy/mysql/init/003-personnel.sql) 和 [004-platform-admins.sql](../../../../registry-deploy/mysql/init/004-platform-admins.sql) 为参考。链接依赖当前同级多项目布局。本阶段不执行 DDL，不复制生产数据；目标库的建立、数据迁移和账号授权留待部署切换。

---

## 部门、员工与管理员

| 契约 | 字段 | 语义 |
| --- | --- | --- |
| `Department` / `departments` | `id`、`name` | 公司顶级部门；真实 ID 沿用钉钉部门 ID，开发模拟部门可使用 0。 |
| `AuthenticatedUser` / `employees` | `userid` | 员工唯一标识，区分大小写，不能为空或包含首尾空白。 |
| 员工身份 | `name` | 员工姓名，历史未知可为 null；同步中间件时回退为 userid。 |
| 员工身份 | `department_id`、`department_name` | 员工所属的唯一顶级部门 ID 和名称。 |
| 员工身份 | `avatar_url` | 对外资料中仅为 32 位小写十六进制标识加 `.webp` 文件名，无头像为 null。 |
| 员工身份 | `platform_permissions` | 平台代码到非负整数文件读取权限等级的映射，未配置的平台无授权。 |
| 员工身份 | `managed_platforms` | 来自管理员关联的去重、排序后平台代码列表，不从文件权限等级推导。 |
| `PlatformAdmin` / `platform_admins` | `platform_code`、`employee_userid` | 一个平台可关联多名员工，同一员工可管理多个平台。 |
| `PlatformAdminProfile` | `name`、`avatar_url` | 管理员展示资料，无姓名时回退 userid；头像仍仅为文件名或 null。 |

平台代码格式为 `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`。文件访问等级和平台管理权限独立：等级为 0 的员工也可拥有管理权限；等级较高不会自动成为管理员。

**缺失平台键表示未授权：** `platform_permissions` 中没有某个平台键时，不得将其等同于显式等级 0，也不得自动授予访问。后续鉴权必须先确认平台键存在，再按该平台规则判断明确记录的等级；显式 0 本身也不表示无条件访问或管理权限。

新员工仅为中间件目录可见平台（含离线平台）及自身初始化等级 0，不虚构其他隐藏平台键。已有员工再次登录不重置或补全权限，不因目录显示/隐藏变化自动增加平台键或提升等级；如将来需要完整隐藏平台目录，由用户单独协调外部契约，本阶段不扩展。

数据库头像字段使用内部引用 `user/avatar/<32位标识>.webp`，通过 `avatar_response_filename()` 校验并映射为文件名。禁止把本机目录、URL 或接口前缀写入响应头像引用。

---

## 中间件资料映射

专用 [MiddlewareUserInfo](../../../app/model/middleware_user.py) 只允许三个字段：

| 字段 | 来源 |
| --- | --- |
| `userid` | 员工 userid，保持大小写。 |
| `username` | 员工 name；历史未知时使用 userid。 |
| `permissions` | 员工 platform_permissions。 |

部门、头像、管理身份和任何用户 token 均不进入该 DTO。全表从与 registry 权限编辑相同的权威人员源读取，镜像已有权限，不从登录对象或模拟配置重算旧员工等级；资料增量、权限与 SQL 管理员关系编辑及管理员增量维护只在 registry；管理员启动全量镜像已由知识后端承担，见[管理员同步契约](../../capability/application/startup-admin-sync.md#外部调用契约)。权威源确认及跨后端旧快照覆盖限制见[职责与运维边界](../system/user-auth.md#中间件维护方授权与运维状态)。平台 Bearer 仅用于服务端 HTTP 认证头，不属于用户对象。

---

## 当前实现边界

仓库提供员工、顶级部门和管理员关联查询，并准备显式调用的员工初始化事务。读取失败、非法 JSON 和字段校验失败必须上抛，不返回空表代替故障。员工初始化不覆盖已存在员工的姓名、头像、部门或权限。

钉钉部门 HTTP 适配保留 `dept_id`、`parent_id`、`name`，员工适配保留 `userid`、`name`、`dept_id_list`、`avatar`。唯一顶级部门解析与首次登录初始化已由[登录与会话管理](../system/user-auth.md#首次员工初始化)实现。登录与当前身份路由见[用户认证 API](../../api/auth.md)，数据契约不承担 token 签发或登录创建。
