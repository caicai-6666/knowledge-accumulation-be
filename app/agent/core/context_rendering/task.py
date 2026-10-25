"""单任务输入展示；沿用参考项目分区，显示业务时间基准，不注入树或节点标识。"""
from datetime import datetime, timedelta, timezone
from .block import text_block,metadata_block
from .attachment import render_attachments


def render_task_input(question,*,task_number,attachments=(),supplemental='',platform_permissions=None,created_at=None):
    if type(task_number) is not int or task_number<1:raise ValueError('展示序号必须为正整数')
    if not isinstance(question,str) or not question.strip():raise ValueError('用户问题不能为空')
    # 序号只表示本次上下文中的阅读顺序，不使用持久化节点编号。
    lines=['━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',f'任务 {task_number}',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━']
    if created_at is not None:
        if not isinstance(created_at,datetime) or created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError('任务创建时间必须包含时区')
        stamp=created_at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
        lines.append(f'任务创建时间：{stamp}（北京时间）')
    lines+=['','## 用户输入','','### 用户问题','',text_block(question)]
    attachment_text=render_attachments(attachments)
    if attachment_text:
        lines+=['',attachment_text]
    if supplemental:
        lines+=['','### 上下文资料','',text_block(supplemental)]
    if platform_permissions:
        lines+=['','### 平台访问等级','',metadata_block(dict(sorted(platform_permissions.items())))]
    return '\n'.join(lines)


def render_task_ending(status,*,task_number):
    labels={'complete':'已完成','cancelled':'用户终止','superseded':'用户调整方向','failed':'执行失败'}
    return f'任务结束记录：第 {task_number} 轮；{labels[status]}。这是历史状态，不是新的用户请求。'
