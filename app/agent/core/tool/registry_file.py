"""注册中心PDF图像查看工具；文件ID与用户上传UUID明确区分。"""
from pydantic import BaseModel,ConfigDict,Field,field_validator
from app.infrastructure.registry_file import validate_resource_id
from .registry import RegisteredTool
from .progress import ToolProgress


class ViewRegistryFileArguments(BaseModel):
    """当你需要阅读注册中心文件的原始页面、核对具体内容或继续翻页时，使用这个工具。只支持PDF，按多图逐页返回原生图像，不支持音频和视频。先通过文件查询及候选列表获得目标文件ID，再原样填入file_id；不要填写来源平台自己的ID、用户上传附件UUID、文件名或路径。每次查看均按当前用户权限读取原件。可以指定页码，也可以省略以继续下一页；失败不推进阅读位置。需要继续使用的事实、来源及限制应保存到工作区；不能仅凭查看记录推断原文。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    file_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$',
        description='文件查询列表中“文件 ID”的完整十进制字符串，范围1至18446744073709551615，是注册中心正式资源ID。不要传整数JSON值、source_id、上传UUID、文件名、路径或自行猜测的ID。')
    page_number: int | None = Field(default=None,gt=0,
        description='从1开始的文件页码，按页面顺序而非印刷页码。省略或null读取本轮上次成功查看该文件的下一页，首次为第1页；显式页码可重看，越界报错且阅读位置不变。')

    @field_validator('file_id')
    @classmethod
    def valid_id(cls,value):
        return validate_resource_id(value)


def build_registry_file_registration(viewer):
    async def progress(args):
        prefix='正在查看文件'
        try:
            name=await viewer.file_name(args.file_id)
        except Exception:
            # 展示资料失败不泄漏名称或底层错误；实际查看仍独立鉴权并返回原有错误契约。
            name=None
        if name:
            text=''.join(char for char in ' '.join(name.split()) if char.isprintable())
            if text:
                limit=2000-len(prefix)-1
                if len(text)>limit:text=text[:limit-1]+'…'
                prefix+='：'+text
        return ToolProgress(type='thinking',message=prefix)

    return RegisteredTool('view_registry_file',ViewRegistryFileArguments.__doc__,ViewRegistryFileArguments,
        lambda name,raw,workspace,args:workspace,executor=lambda args:viewer.view(**args.model_dump()),
        return_types=('foldable',),page_resolver=viewer.resolve_page,
        progress=ToolProgress(type='thinking',message='正在查看文件'),progress_factory=progress)
