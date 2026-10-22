"""任务附件的稳定引用与宿主上传输入；磁盘后缀由语义类型唯一确定。"""
from typing import Annotated,Literal
from pydantic import BaseModel,ConfigDict,Field

FileType=Literal['images','audio','video']
FILE_EXTENSIONS={'images':'.pdf','audio':'.mp3','video':'.mp4'}
UUID_PATTERN=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
FileID=Annotated[str,Field(strict=True,pattern=UUID_PATTERN)]


class AttachmentReference(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    type: FileType = Field(description='附件数据类型：images 为多图，audio 为音频，video 为视频。')
    file_id: FileID = Field(description='服务端分配的完整小写带连字符 UUID 文件标识。')

    @property
    def filename(self):
        return self.file_id+FILE_EXTENSIONS[self.type]


class UploadedFile(BaseModel):
    """调用方已按约定准备格式；文件存储层不负责转码或解析。"""
    model_config=ConfigDict(extra='forbid',frozen=True)
    type: FileType
    content: bytes = Field(strict=True,min_length=1,repr=False)


def unique_references(values):
    if len({item.file_id for item in values})!=len(values):
        raise ValueError('同一任务的文件 UUID 不能重复')
    return values
