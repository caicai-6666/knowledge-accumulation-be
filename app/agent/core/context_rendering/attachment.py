"""公共附件引用展示；不推断文件格式，也不绑定读取工具。"""
from pydantic import TypeAdapter
from ..model import AttachmentReference
from .block import metadata_block

_REFERENCES=TypeAdapter(list[AttachmentReference])
_TYPE_LABELS={'images':'多图','audio':'音频','video':'视频'}


def render_attachments(attachments):
    references=_REFERENCES.validate_python(attachments)
    if not references:return ''
    # 显式投影仅有的两个展示字段，避免将宿主文件元数据带入模型上下文。
    data=[{'文件类型':_TYPE_LABELS[item.type],'文件ID':item.file_id} for item in references]
    return '### 用户附件\n\n'+metadata_block(data)
