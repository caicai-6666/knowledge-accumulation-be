"""员工背景和助理目录共用的文本展示；不修改权威资料。"""

from collections.abc import Sequence

from app.model.personnel import Department


def employee_field(value: str | None) -> str:
    # 保持外部字段单行，避免换行伪造其他用户或字段；不改数据库原值。
    return value.replace('\r', '\\r').replace('\n', '\\n') if value is not None else '未知'


def render_departments(departments: Sequence[Department], *, legacy_name: str | None = None) -> str:
    names = [item.name for item in sorted(departments, key=lambda item: item.id)]
    # 仅为空的旧模拟身份回退；真实 SQL 已将历史单部门转换为完整列表。
    if not names and legacy_name is not None:
        names = [legacy_name]
    if not names:
        return '所属一级部门：未知'
    return '所属一级部门：\n' + '\n'.join('· ' + employee_field(name) for name in names)
