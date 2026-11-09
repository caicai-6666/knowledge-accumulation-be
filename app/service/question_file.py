"""探索附件保存与读取；元信息登记仅由个人助理上传流程负责。"""
import asyncio
import logging
from pydantic import TypeAdapter
from app.repository.question_tree import QuestionTreeNotFound

logger=logging.getLogger(__name__)
from app.model.question_file import AttachmentReference,UploadedFile,unique_references
from app.repository.node_persistence import QuestionFileNotFound

_UPLOADS=TypeAdapter(list[UploadedFile])
_REFERENCES=TypeAdapter(list[AttachmentReference])


class QuestionFileService:
    def __init__(self,nodes,store):
        self.nodes=nodes
        self.store=store

    async def prepare(self,tree_id,owner_userid,*,uploads=(),references=()):
        uploads=_UPLOADS.validate_python(uploads)
        references=unique_references(_REFERENCES.validate_python(references))
        # 整批普通校验和归属校验在写文件前完成；引用既有文件时不能跨树借用 UUID。
        for upload in uploads:self.store.validate_upload(upload)
        await self.nodes.trees.load_tree(tree_id,owner_userid)
        for reference in references:
            if await self.nodes.find_file(tree_id,owner_userid,reference.file_id)!=reference:
                raise QuestionFileNotFound('附件类型与登记不一致')
        created=[]
        try:
            for upload in uploads:
                # to_thread 写盘不可被协程取消；等待其结果后补偿，避免 UUID 丢失留下孤儿文件。
                saving=asyncio.create_task(self.store.save(upload))
                try:
                    created.append(await asyncio.shield(saving))
                except asyncio.CancelledError:
                    created.append(await saving)
                    raise
        except BaseException:
            for reference in created:
                try:
                    await self.store.delete(reference)
                except Exception as error:
                    # 文件删除失败时保留初始异常，避免补偿错误覆盖上传失败原因。
                    logger.warning('上传文件补偿未确认：error_type=%s',type(error).__name__)
            raise
        # 探索附件不写 uploaded_files；调用方仍须与任务一起登记附件引用。
        return unique_references([*references,*created])

    async def cleanup_unregistered(self,tree_id,owner_userid,references):
        for reference in references:
            try:
                try:
                    await self.nodes.find_file(tree_id,owner_userid,reference.file_id)
                except (QuestionFileNotFound,QuestionTreeNotFound):
                    await self.store.delete(reference)
            except Exception as error:
                logger.warning('附件补偿未确认：error_type=%s',type(error).__name__)

    async def read_file(self,tree_id,owner_userid,file_id):
        reference=await self.nodes.find_file(tree_id,owner_userid,file_id)
        content=await self.store.read(reference)
        # 文件 I/O 不占数据库锁；结束后重新校验，删除或归属变化不能沿用旧授权。
        if await self.nodes.find_file(tree_id,owner_userid,file_id)!=reference:
            raise QuestionFileNotFound('附件登记已发生变化')
        return reference,content
