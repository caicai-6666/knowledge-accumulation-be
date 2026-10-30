"""为当前所属用户提供最小员工事实背景，不加载全员资料或其他助理的画像。"""

from app.agent.assistant.context import AssistantContext, ContextMaterial
from app.agent.assistant.rendering import employee_field as display, render_departments
from app.auth.model import AuthenticatedUser
from app.repository.employee import MySQLEmployeeRepository


class AssistantEmployeeContextLoader:
    def __init__(self, employees: MySQLEmployeeRepository):
        self._employees = employees

    async def __call__(self, user: AuthenticatedUser) -> AssistantContext:
        # user 由消费宿主从 SQL 权威员工表取得；模型不能指定当前资料的所属用户。
        profile = await self._employees.find_work_profile(user.userid)
        if profile is None:
            raise ValueError('当前员工资料已不存在，不能继续加载个人背景')
        manager = None
        if profile.manager_userid is not None:
            manager = await self._employees.find_by_userid(profile.manager_userid)
        information = '\n'.join((
            '════════════ 所属用户信息 ════════════',
            '',
            '姓名：' + display(user.name),
            '用户ID：' + display(user.userid),
            '',
            render_departments(user.departments, legacy_name=user.department_name),
            '职位：' + display(profile.title),
            '',
            '所属主管：' + display(manager.name if manager is not None else None),
            '所属主管ID：' + display(profile.manager_userid),
            '',
            '════════════ 用户信息结束 ════════════',
        ))
        return AssistantContext(profile=[ContextMaterial(
            content=information, source='企业员工资料：当前已保存的姓名、部门和任职记录',
            status='confirmed')])
