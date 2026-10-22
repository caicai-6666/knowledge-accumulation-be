"""UUID 文件的独占落盘和受限读取；不依赖 HTTP、数据库或文件转换工具。"""
import asyncio
import os
from pathlib import Path
import stat
from uuid import uuid4
from app.config.middleware import PROJECT_ROOT
from app.model.question_file import AttachmentReference,UploadedFile


class UploadFileStore:
    def __init__(self,root:Path=PROJECT_ROOT/'data/user/upload-file',*,max_bytes:int=52428800):
        if type(max_bytes) is not int or max_bytes<=0:raise ValueError('文件大小限制必须是正整数')
        # 不 resolve 符号链接；所有路径分量都由目录描述符逐级打开。
        self.root=Path(root).absolute()
        if '..' in self.root.parts:raise ValueError('文件根目录不能包含父目录跳转')
        self.max_bytes=max_bytes

    def _directory(self,*,create=False):
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        descriptor=os.open(self.root.anchor,flags)
        try:
            for part in self.root.parts[1:]:
                try:next_descriptor=os.open(part,flags,dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:raise
                    try:os.mkdir(part,mode=0o700,dir_fd=descriptor)
                    except FileExistsError:pass
                    next_descriptor=os.open(part,flags,dir_fd=descriptor)
                os.close(descriptor)
                descriptor=next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def validate_upload(self,upload):
        upload=UploadedFile.model_validate(upload.model_dump() if isinstance(upload,UploadedFile) else upload)
        if len(upload.content)>self.max_bytes:raise ValueError('上传文件超出大小限制')
        return upload

    async def save(self,upload:UploadedFile) -> AttachmentReference:
        upload=self.validate_upload(upload)
        return await asyncio.to_thread(self._save,upload)

    def _save(self,upload):
        directory=self._directory(create=True)
        try:
            for _ in range(8):
                reference=AttachmentReference(type=upload.type,file_id=str(uuid4()))
                try:
                    descriptor=os.open(reference.filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
                        0o600,dir_fd=directory)
                except FileExistsError:continue
                try:
                    with os.fdopen(descriptor,'wb') as handle:
                        handle.write(upload.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.fsync(directory)
                    return reference
                except BaseException:
                    os.unlink(reference.filename,dir_fd=directory)
                    raise
            raise FileExistsError('无法分配唯一上传文件 UUID')
        finally:os.close(directory)

    async def read(self,reference:AttachmentReference) -> bytes:
        reference=AttachmentReference.model_validate(reference.model_dump())
        return await asyncio.to_thread(self._read,reference)

    async def delete(self, reference: AttachmentReference) -> None:
        """按数据库清单删除固定文件名；缺失视为已清理，拒绝符号链接和非普通文件。"""
        reference = AttachmentReference.model_validate(reference.model_dump())
        await asyncio.to_thread(self._delete, reference)

    def _delete(self, reference):
        try:
            directory = self._directory()
        except FileNotFoundError:
            return
        try:
            try:
                info = os.stat(reference.filename, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('待删除附件不是普通文件')
            try:
                os.unlink(reference.filename, dir_fd=directory)
            except FileNotFoundError:
                pass  # 并发重试可能已经完成同一清单中的文件。
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read(self,reference):
        directory=self._directory()
        try:
            descriptor=os.open(reference.filename,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(descriptor,'rb') as handle:
                info=os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=self.max_bytes:
                    raise ValueError('上传文件类型或大小不合法')
                content=handle.read(self.max_bytes+1)
                if not content or len(content)>self.max_bytes or len(content)!=info.st_size:
                    raise ValueError('上传文件为空、超限或读取期间发生变化')
                return content
        finally:os.close(directory)
