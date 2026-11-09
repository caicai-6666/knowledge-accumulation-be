"""个人文件备忘增删；参数与程序结果分离，独立末尾钩子负责反馈。"""
import logging
from textwrap import dedent, indent
from pydantic import Field, TypeAdapter, field_validator
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.json_codec import ModelActionRejected
from app.agent.core.tool import RegisteredTool, ToolExecutionResult
from app.agent.core.tool.feedback import with_feedback
from app.model.assistant_file_note import NoteName, NoteSummary
from app.model.question_file import FileID
from app.model.uploaded_file import FileOwnerID
from app.repository.assistant_file_note import FileNoteRejected, FileNoteWriteUncertain

logger = logging.getLogger(__name__)


class AddFileNoteArguments(WorkspaceObject):
    """需要为所属用户上传的附件建立可查找备忘时使用。输入已确认的文件 ID、名称和摘要，成功返回记录内容及时间；例如将用户指定的采购合同记为“2026年采购合同”。记录者由当前助理身份确定，不能为他人上传的文件新增。每用户每文件最多一条，已有记录不覆盖；不要自动删除重建。名称和摘要须有依据，私密文件可根据用户提供的信息记账，但不能假装已经读取正文，也不自动公开文件。失败按具体反馈修正，结果不确定时停止重复操作，请管理员核对。"""
    file_id: FileID = Field(description='所属用户上传文件的完整小写带连字符 UUID，来自已确认附件或用户提供；不能填任务 ID、文件名或猜测值。')
    name: NoteName = Field(description='便于以后查找的文件备忘名称，去除首尾空白后为 1 至 200 字符；根据实际内容拟定或与用户确认。')
    summary: NoteSummary = Field(description='有依据的文件摘要及用途，去除首尾空白后不能为空，UTF-8 编码最多 65535 字节；未读取时只记录用户明确提供的信息，不编造内容。')

    @field_validator('summary')
    @classmethod
    def capacity(cls, value):
        if len(value.encode('utf-8')) > 65535:
            raise ModelActionRejected('备忘摘要超过 UTF-8 65535 字节限制，本次尚未新增。',
                '精简 summary，保留有依据的关键内容和用途，再重新提交。')
        return value


class DeleteFileNoteArguments(WorkspaceObject):
    """需要删除所属用户的一条文件备忘时使用，例如用户要求不再保留某文件的索引记录。填写准确文件 ID，只删除本人备忘，不删除原文件、上传元信息或其他人的备忘，不改变可见范围。无需先查询，不要求文件公开；没有记录时返回无需删除。不要为解决新增重复错误而自动删除；结果不确定时停止重复操作并请管理员核对。"""
    file_id: FileID = Field(description='需要删除备忘的完整小写带连字符 UUID 文件 ID；原样使用已确认的目标，不填名称、任务 ID 或猜测值。仅删除当前所属用户的记录。')


def render_add_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status == 'succeeded':
        text = dedent("""
            ### 文件备忘新增

            状态：已创建
            文件 ID：{file_id}
            名称：{name}
            摘要：
            {summary}
            文件上传时间：{uploaded_at}
            备忘更新时间：{updated_at}
        """).strip().format(**{**result,
            'name':result['name'].replace('\r', '\\r').replace('\n', '\\n'),
            'summary':indent(result['summary'], '> ', predicate=lambda line: True)})
    else:
        text = dedent("""
            ### 文件备忘新增

            状态：{state}
            原因：{reason}
            下一步：{required_action}
        """).strip().format(**result, state='结果尚未确认，已停止继续执行' if execution.result.status == 'unknown' else '未新增')
    return with_feedback(execution, text)


def render_delete_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status == 'succeeded':
        text = dedent("""
            ### 文件备忘删除

            状态：{state}
            文件 ID：{file_id}
        """).strip().format(**result, state='已删除' if result['deleted'] else '备忘已不存在，无需删除')
    else:
        text = dedent("""
            ### 文件备忘删除

            状态：{state}
            原因：{reason}
            下一步：{required_action}
        """).strip().format(**result, state='结果尚未确认，已停止继续执行' if execution.result.status == 'unknown' else '未删除')
    return with_feedback(execution, text)


class FileNoteTools:
    def __init__(self, repository, owner_userid):
        self.repository = repository
        self.owner_userid = TypeAdapter(FileOwnerID).validate_python(owner_userid)

    async def add(self, args):
        note, uploaded_at = await self.repository.add(self.owner_userid, **args.model_dump())
        return ToolExecutionResult(status='succeeded', tool_result={
            'file_id':note.file_id, 'name':note.name, 'summary':note.summary,
            'uploaded_at':uploaded_at.isoformat(), 'updated_at':note.updated_at.isoformat()})

    async def delete(self, args):
        deleted = await self.repository.delete(self.owner_userid, args.file_id)
        return ToolExecutionResult(status='succeeded', tool_result={'file_id':args.file_id, 'deleted':deleted})

    def registrations(self):
        def build(name, model, execute, render):
            async def guarded(args):
                try:
                    return await execute(args)
                except FileNoteRejected as error:
                    return ToolExecutionResult(status='failed', tool_result={
                        'reason':error.reason, 'required_action':error.required_action})
                except FileNoteWriteUncertain as error:
                    return ToolExecutionResult(status='unknown', tool_result={
                        'reason':str(error), 'required_action':'请由管理员核对记录，不要自动重复操作。'})
                except Exception as error:
                    logger.warning('文件备忘操作准备失败：tool=%s error_type=%s', name, type(error).__name__)
                    return ToolExecutionResult(status='failed', tool_result={
                        'reason':'文件备忘操作前的检查未完成，本次未执行变更。',
                        'required_action':'请稍后重试，不能据此判断备忘已创建、删除或不存在。'})
            return RegisteredTool(name, model.__doc__, model,
                lambda name, raw, workspace, arguments: workspace, executor=guarded, hooks=(render,))
        return (build('add_file_note', AddFileNoteArguments, self.add, render_add_feedback),
                build('delete_file_note', DeleteFileNoteArguments, self.delete, render_delete_feedback))
