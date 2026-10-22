"""独立图像文件阅读工具；只查看多图附件的页面，不处理音频或视频。"""
from pydantic import BaseModel,ConfigDict,Field
from app.model.question_file import FileID
from .registry import RegisteredTool


class ViewImageFileArguments(BaseModel):
    """当你需要阅读用户图像附件的原始页面、核对具体内容或继续翻页时，使用这个工具。只用于多图附件，不读取音频或视频。一次返回一页图像及当前页、总页数，帮助依据实际页面作答。先在当前任务或历史任务的附件中找到目标文件，将其“文件ID”填入 file_id。可以指定页码，也可以省略以继续查看下一页；查看失败时会说明原因，阅读位置保持不变。需要后续继续使用的事实、来源和限制应保存到工作区；需要核对细节时重新查看，不能仅凭查看记录推断原文。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    file_id:FileID=Field(description='目标多图附件下“文件ID”的完整值，直接原样复制，为小写带连字符的 UUID；不要填写附件序号、文件名或路径，也不要使用音频或视频的文件ID。尚未获得文件ID时不要猜测。')
    page_number:int|None=Field(default=None,gt=0,
        description='要查看的页码，填从1开始的整数，按文件页面顺序计数，不按正文印刷页码。省略或填 null 时，查看本轮任务上次成功查看该文件的下一页；从未查看则打开第1页。例如上次看了第3页，不填就打开第4页；重看第3页需填写3。超过总页数会报错，阅读位置保持不变。')


def build_image_view_registration(viewer):
    def prepare(name,raw,workspace,arguments):return workspace
    async def execute(arguments):return await viewer.view(**arguments.model_dump())
    return RegisteredTool('view_image_file',ViewImageFileArguments.__doc__.strip(),ViewImageFileArguments,
        prepare,executor=execute,return_types=('foldable',),page_resolver=viewer.resolve_page)
