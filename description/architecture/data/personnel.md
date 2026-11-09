# 人员与权限数据契约

> **当前部署入口：** 新空库仅执行 deploy 的[最新初始化脚本](../../../../knowledge-accumulation-deploy/mysql/init/001-schema.sql)。本文中的编号迁移和旧库操作属于历史演进记录，不是新部署步骤；历史 SQL 已从当前仓库移除，仅可从 Git 历史追溯。已有数据库须另行制定升级方案，不可重跑初始化。

## 适用范围与来源

本契约用于新后端的用户基础设施及全表注册。沿用来源项目的人员字段语义，不迁入注册中心的资源、审核表或工作流。仓库和运行配置见[用户基础设施](../../capability/infrastructure/user-infrastructure.md)，注册发布规则见[用户全表注册协调](../../capability/application/startup-user-sync.md)。

表结构以部署项目的 [003-personnel.sql](../../../../knowledge-accumulation-deploy/mysql/init/003-personnel.sql) 和 [004-platform-admins.sql](../../../../knowledge-accumulation-deploy/mysql/init/004-platform-admins.sql) 为参考。链接依赖当前同级多项目布局。人员扩展迁移由本项目 `schema/` 维护，部署时手工执行；应用启动不执行 DDL，不复制生产数据；目标库的建立、数据迁移和账号授权留待部署切换。

---

## 部门、员工与管理员

| 契约 | 字段 | 语义 |
| --- | --- | --- |
| `Department` / `departments` | `id`、`name` | 公司顶级部门；真实 ID 沿用钉钉部门 ID，开发模拟部门可使用 0。 |
| `AuthenticatedUser` / `employees` | `userid` | 员工唯一标识，区分大小写，不能为空或包含首尾空白。 |
| 员工身份 | `name` | 员工姓名，历史未知可为 null；同步中间件时回退为 userid。 |
| 员工身份 | `employment_status` | 在职状态：`employed` 在职、`resigned` 离职；首次创建默认在职。 |
| 员工身份 | `department_id`、`department_name` | 旧版兼容部门 ID 和名称；完整归属见 departments 列表。 |
| 员工身份 | `avatar_url` | 对外资料中仅为 32 位小写十六进制标识加 `.webp` 文件名，无头像为 null。 |
| 员工身份 | `platform_permissions` | 平台代码到非负整数文件读取权限等级的映射，未配置的平台无授权。 |
| 员工存储 | `work_profile` | 任职资料 JSON，含直属主管 `manager_userid`、职位 `title`。 |
| 员工身份 | `managed_platforms` | 来自管理员关联的去重、排序后平台代码列表，不从文件权限等级推导。 |
| `PlatformAdmin` / `platform_admins` | `platform_code`、`employee_userid` | 一个平台可关联多名员工，同一员工可管理多个平台。 |
| `PlatformAdminProfile` | `name`、`avatar_url` | 管理员展示资料，无姓名时回退 userid；头像仍仅为文件名或 null。 |

平台代码格式为 `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`。文件访问等级和平台管理权限独立：等级为 0 的员工也可拥有管理权限；等级较高不会自动成为管理员。

**缺失平台键表示未授权：** `platform_permissions` 中没有某个平台键时，不得将其等同于显式等级 0，也不得自动授予访问。后续鉴权必须先确认平台键存在，再按该平台规则判断明确记录的等级；显式 0 本身也不表示无条件访问或管理权限。

新员工为中间件目录返回的平台（含离线平台）及自身初始化等级 0，不推断目录以外的平台键。当前上游目录返回全部已配置平台；具体范围见[目录与权限边界](../system/user-auth.md#首次员工初始化)。已有员工再次登录不重置或补全权限，不因目录变化自动增加平台键或提升等级。

数据库头像字段使用内部引用 `user/avatar/<32位标识>.webp`，通过 `avatar_response_filename()` 校验并映射为文件名。禁止把本机目录、URL 或接口前缀写入响应头像引用。

---

## 任职资料与初始化

[015 任职资料迁移](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/015-employee-work-profile.sql)在权威人员库为 `employees` 增加 `work_profile` JSON 字段。部署新版初始化代码前需手工执行一次；本轮未对真实数据库执行迁移。数据库与 `EmployeeWorkProfile` 同时约束两个固定键，历史员工的默认值均为 null。

```json
{
  "manager_userid": "manager-a",
  "title": "研发工程师"
}
```

首次钉钉初始化从用户详情读取 `manager_userid` 和 `title`，空白值归一为空；资料与员工、部门在同一事务内写入。直属主管存企业 userid，不以部门负责人替代。已有员工不重新请求钉钉、不补采、不覆盖；开发模拟身份未接钉钉，不虚构任职资料。

`MySQLEmployeeRepository.find_work_profile(userid)` 提供内部读取：员工不存在返回 None，字段非法或数据库异常上抛。任职资料不进入 `AuthenticatedUser`、登录/me 响应或中间件同步；后续消费需在可信业务层确认目标身份和访问范围。成员信息读取使用 `qyapi_get_member`，需覆盖目标员工。用户已取消岗位说明书采集，本字段只保存上述两项；不增加智能人事接口、权限或配置。

---

## 员工在职状态

[022 在职状态迁移](https://github.com/caicai-6666/knowledge-accumulation-be/blob/350b1e2/schema/022-employee-employment-status.sql)为 `employees` 增加独立的 `employment_status` 列，不并入任职 JSON。值仅允许 `employed`、`resigned`，数据库默认 `employed`，拒绝 null 和其他值；身份模型使用同样的枚举与默认值。新员工插入时采用数据库默认值；重复建档只确认既有记录，不会将离职状态重置为在职。

**部署前须先在权威人员库手工执行 022，应用不自动迁移。** 迁移将现有员工初始化为在职，这是兼容默认值，不代表已核实历史任职情况；实际已离职人员需要根据权威名单维护为 `resigned`。本项目没有新增管理编辑 API，可由现有人员库维护流程按精确 userid 更新该字段。例如：

```sql
UPDATE employees SET employment_status='resigned' WHERE userid='目标员工userid';
```

员工查询、全表读取及登录/me 身份返回该字段；个人助理按宿主每轮读取的 SQL 员工身份显示“在职状态：在职／离职”，详见[员工事实资料](../workflow/question-answering/assistant-personal-context.md#员工事实资料)。开发模拟员工默认在职。缺字段或非法存储值属于部署或数据错误，仓库不会静默伪装为在职。

状态用于资料展示及个人助理最终通知校验：所属员工离职时，`finish_task.send_to_user=true` 在提交前被拒绝，须改为 false 静默保存，详见[交付门禁](../../capability/application/assistant-consumption.md#finish_task)。不自动禁用登录、助理间通信或跨平台 A2A；同步边界见[中间件资料映射](#中间件资料映射)。本次不修改注册中心、中间件或 deploy 项目。

2026-11-02 验证：员工状态、建档、助理背景、多部门及用户同步隔离测试共 33 项通过，覆盖四类助理任务的实际上下文构建、离职员工保留在同步集合中和现有出站 DTO 边界。[专项测试](../../../tests/test_employee_employment_status.py)另准备 3 项显式启用的 MySQL 用例；一次性 MySQL 8.4 容器因 Docker OCI runtime 创建失败而未启动，实库迁移及数据库约束尚未验证。失败容器已清理，未连接业务数据库，未执行真实 022 迁移或重启服务。

2026-11-02T12:04:03+08:00 经用户授权，已在本项目当前配置的 `127.0.0.1:13306 / registry`（MySQL 8.4.13）执行 `022-employee-employment-status.sql`。迁移前字段不存在，迁移后核验为 `VARCHAR(16)`、`ascii_bin`、非空、默认 `employed`，`chk_employees_employment_status` 已启用。现有 7 名员工均按默认值初始化为在职，未根据推测标记离职人员。实际员工全表与助理目录仓库读取均返回 7 条，状态一致；未向业务库插入测试记录，未修改其他项目文件、重启服务或主动发布中间件用户总表。该记录仅说明此数据库已迁移，不代表其他环境已同步。

---

## 中间件资料映射

用户全表同步继续包含在职和离职员工；`employment_status` 使用本地与中间件共享的取值 `employed` / `resigned`，原样发送，不做状态映射。

专用 [MiddlewareUserInfo](../../../app/model/middleware_user.py) 只允许五个字段：

| 字段 | 来源 |
| --- | --- |
| `userid` | 员工 userid，保持大小写。 |
| `username` | 员工 name；历史未知时使用 userid。 |
| `permissions` | 员工 platform_permissions。 |
| `employment_status` | 员工 employment_status，原样发送 employed（在职）或 resigned（离职）。 |
| `departments` | 全部一级部门名称数组；完整列表优先，缺少时回退旧 department_name，仍未知则为 []。 |

部门名称按本地部门 ID 排序后去除首尾空白并按名称去重；中间件不解释 ID、层级或主部门，同名不同 ID 在本地仍分别保留。DTO 强制提供 departments 和 employment_status，避免全表省略字段导致中央部门被清空或任职状态重置为在职；部门列表拒绝 null、空白名称与重复名称，任职状态仅接受上述两个值。部门归属不自动授予权限。部门 ID、头像、管理身份和任何用户 token 均不进入该 DTO。全表从与 registry 权限编辑相同的权威人员源读取，镜像已有权限，不从登录对象或模拟配置重算旧员工等级；资料增量、权限与 SQL 管理员关系编辑及管理员增量维护只在 registry；管理员启动全量镜像已由知识后端承担，见[管理员同步契约](../../capability/application/startup-admin-sync.md#外部调用契约)。权威源确认及跨后端旧快照覆盖限制见[职责与运维边界](../system/user-auth.md#中间件维护方授权与运维状态)。平台 Bearer 仅用于服务端 HTTP 认证头，不属于用户对象。

---

## 当前实现边界

仓库提供员工、顶级部门和管理员关联查询，并准备显式调用的员工初始化事务。读取失败、非法 JSON 和字段校验失败必须上抛，不返回空表代替故障。员工初始化不覆盖已存在员工的姓名、头像、部门或权限。

钉钉部门 HTTP 适配保留 `dept_id`、`parent_id`、`name`，员工适配保留 `userid`、`name`、`dept_id_list`、`avatar`、`manager_userid`、`title`。多一级部门归并与首次登录初始化已由[登录与会话管理](../system/user-auth.md#首次员工初始化)实现。登录与当前身份路由见[用户认证 API](../../api/auth.md)，数据契约不承担 token 签发或登录创建。


## 助理姓名目录

`MySQLEmployeeRepository.search_user_names(username=None, department=None, offset=0, limit=11)` 返回 userid、展示姓名、employment_status（employed/resigned）与完整一级部门 departments（id/name）列表；在职和离职员工均保留，状态必须来自 SQL，缺失或非法时拒绝结果，不默认推测为在职；复用员工身份查询的部门投影，历史 department_ids 为 NULL 时回退旧 department_id。按员工限量，部门聚合不会增加结果人数；部门按 ID 排列，不按名称合并。姓名和部门均为空时列出全部员工，按展示姓名、userid稳定排序，姓名为null时回退userid；姓名和一级部门名称各自使用全文及字面包含查询；双路取并集并按用户去重、等权排名融合。省略、null、空串、全空白跳过对应条件；非法类型、超长条件和非法分页参数在查询前拒绝。模型工具的数量上限、分页快照与LRU使用规则统一见[助理目录](../../capability/application/assistant-consumption.md#按用户名查找助理)。


---

## 多一级部门归属

员工 `department_ids` 保存全部一级部门ID的去重JSON数组，旧 `department_id`为排序首项兼容投影，无主部门语义。JSON列NULL仅用于历史记录或旧客户端写入，读取时回退旧单值；新建档与刷新写完整数组。JSON检查约束拒绝空数组、重复值及负数；数组成员与部门行由同一事务维护。身份返回 `departments` 完整名称列表，助理背景使用全部名称。

迁移与规则见[用户初始化](../system/user-auth.md#多一级部门存储与兼容)。中央用户同步包含完整部门名称列表，映射规则见[中间件资料映射](#中间件资料映射)。
