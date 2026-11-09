"""图像附件查看器：每次使用核验来源，派生缓存可共享，阅读位置按运行隔离。"""
import asyncio
import base64
from uuid import uuid4
from pydantic import TypeAdapter
from app.model.question_file import AttachmentReference,FileID
from ..tool.result import ToolExecution,ToolExecutionResult,FoldableToolContent,ToolPageReference
from .cache import FileKey,FileModelCache,FileCacheBusyError
from .pdf import FileModel,PDFOpenError,PDFRenderConfig


class FileViewError(ValueError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code


class ImageFileViewer:
    def __init__(self,*,scope,read_file,authorize,cache:FileModelCache,config:PDFRenderConfig|None=None):
        if (not isinstance(scope,tuple) or len(scope)!=2 or not all(isinstance(item,str) and item for item in scope)
                or not callable(read_file) or not callable(authorize) or not isinstance(cache,FileModelCache)):
            raise ValueError('附件查看必须绑定用户、树、读取与授权函数及文件池')
        self.scope=scope
        self.read_file=read_file
        self.authorize=authorize
        self.cache=cache
        self.config=config or PDFRenderConfig()
        self._positions={}
        self._pages={}
        self._lock=asyncio.Lock()
        self._closed=False

    def _require_open(self):
        if self._closed:raise FileViewError('viewer_closed','本次附件查看已结束，请在有效任务中重新查看。')

    async def _source(self,file_id):
        # 上传时为每份文件分配独占 UUID；命中只需检查登记归属，不再读整份 PDF 算哈希。
        reference=await self.authorize(file_id)
        reference=AttachmentReference.model_validate(reference.model_dump())
        if reference.file_id!=file_id:raise FileViewError('invalid_source','附件来源标识不一致，请核对文件引用。')
        if reference.type!='images':
            raise FileViewError('unsupported_type','本工具只阅读图像附件，不支持音频或视频；请提供多图附件的文件引用。')
        return reference,None,FileKey(self.scope,file_id)

    async def _render(self,file_id,number,*,expected=None,next_page=False,source=None):
        self._require_open()
        reference,content,key=source or await self._source(file_id)
        if expected is not None and key!=expected:
            raise FileViewError('file_changed','附件内容已变化，请重新调用查看工具。')
        def page_from(model):
            if number>model.page_count:
                if next_page:raise FileViewError('end_of_file',f'附件已到最后一页，共 {model.page_count} 页；可指定页码重看。')
                raise FileViewError('page_out_of_range',f'页码超出范围，请填写 1 至 {model.page_count} 之间的整数。')
            return model.get_page(number),model.page_count
        def cached():
            with self.cache.acquire(key) as model:
                return page_from(model)
        def loaded():
            with self.cache.acquire_or_load(key,lambda:FileModel(content,config=self.config)) as model:
                return page_from(model)
        if content is None:
            try:
                page,count=await asyncio.to_thread(cached)
            except KeyError:
                # 只有未命中（含淘汰后重开）才读盘；加载期间再次核对登记，防止来源变化。
                loaded_reference,content=await self.read_file(file_id)
                if loaded_reference!=reference:
                    raise FileViewError('file_changed','附件登记已变化，请重新核对文件引用。')
                if not isinstance(content,bytes):raise FileViewError('invalid_file','未取得有效附件内容，请重新上传可读取的文件。')
                page,count=await asyncio.to_thread(loaded)
        else:
            # 远程来源已完成下载及授权，沿用其版本键，不改变注册中心访问契约。
            page,count=await asyncio.to_thread(loaded)
        current=await self.authorize(file_id)
        self._require_open()
        if current!=reference:raise FileViewError('file_changed','附件登记已变化，请重新核对文件引用。')
        return key,page,count

    @staticmethod
    def _error(error):
        if isinstance(error,FileViewError):return error
        if isinstance(error,(PermissionError,LookupError)):
            return FileViewError('file_unavailable','附件不存在或当前用户无权访问，请核对文件引用。')
        if isinstance(error,PDFOpenError):return FileViewError('invalid_file','附件未能解析为可读取的图像页面，请提供可读取的文件。')
        if isinstance(error,FileCacheBusyError):return FileViewError('resource_busy','附件暂时无法打开，请稍后重试。')
        if isinstance(error,(ValueError,TypeError)):return FileViewError('invalid_arguments','附件读取未通过校验，请核对文件引用和页码后重新查看。')
        return FileViewError('read_failed','附件暂时无法读取，请稍后重试。')

    def _validate_file_id(self, file_id):
        return TypeAdapter(FileID).validate_python(file_id)

    def _page_description(self, file_id, target, count):
        return f'文件类型: 多图\n文件ID: {file_id}\n页码: {target}\n总页数: {count}'

    async def view(self,file_id,page_number=None):
        async with self._lock:
            try:
                self._require_open()
                file_id=self._validate_file_id(file_id)
                if page_number is not None and (type(page_number) is not int or page_number<1):
                    raise FileViewError('invalid_arguments','页码须为从 1 开始的整数；省略表示下一页。')
                source=await self._source(file_id)
                target=page_number if page_number is not None else self._positions.get(source[2],0)+1
                key,_,count=await self._render(file_id,target,expected=source[2],next_page=page_number is None,source=source)
                # 每次重看都有新展示身份；完整页面始终由注册解析器按引用再授权读取。
                reference=ToolPageReference(resource_id=file_id,display_id=str(uuid4()),
                    locator=f'page:{target}',media_type='image',
                    description=self._page_description(file_id,target,count))
                def committed():
                    self._require_open()
                    self._positions[key]=target
                    self._pages[reference.display_id]=(reference,key,target)
                return ToolExecution(ToolExecutionResult(status='succeeded',
                    tool_result={'status':'accepted','result':{
                        'status':'success','file_id':file_id,'page_number':target,'page_count':count,
                        'message':'这是查看记录，不包含原文；需要核对时请重新调用查看工具。'}},
                    content=FoldableToolContent(pages=[reference])),after_commit=committed)
            except Exception as error:
                error=self._error(error)
                return ToolExecution(ToolExecutionResult(status='failed',tool_result={
                    'status':'error','code':error.code,'error':str(error)}))

    async def resolve_page(self,reference):
        async with self._lock:
            try:
                self._require_open()
                reference=ToolPageReference.model_validate(reference)
                issued=self._pages.get(reference.display_id)
                if issued is None or issued[0]!=reference:
                    raise FileViewError('invalid_reference','页面引用无效，请重新查看文件。')
                _,key,number=issued
                _,page,_=await self._render(key.file_id,number,expected=key)
                encoded=await asyncio.to_thread(lambda:base64.b64encode(page.png).decode('ascii'))
                return [{'type':'image_url','image_url':{'url':'data:image/png;base64,'+encoded}}]
            except Exception as error:raise self._error(error) from None

    async def aclose(self):
        async with self._lock:
            self._closed=True
            self._positions.clear()
            self._pages.clear()
