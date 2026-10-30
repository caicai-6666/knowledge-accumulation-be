"""画像增删预验；权威写入由助理节点检查点的同事务钩子执行。"""
import logging

from app.agent.core.tool import RegisteredTool, ToolExecutionResult
from app.model.assistant_portrait import PortraitRecord, PortraitSubject
from .portrait_contract import (AddPortraitArguments, AddPortraitResult, DeletePortraitArguments,
    DeletePortraitResult, PORTRAIT_TOOL_CONTRACTS)

logger = logging.getLogger(__name__)


class PortraitMutationTools:
    def __init__(self, repository, owner_userid: str):
        self.repository = repository
        self.owner_userid = PortraitSubject(userid=owner_userid).userid

    async def add(self, args):
        if len(args.preference.encode('utf-8')) > 65535:
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'portrait_too_large', 'message': '这一条偏好过长，请精简后再新增。'})
        if not await self.repository.target_exists(args.subject.userid):
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'portrait_target_unavailable', 'message': '未找到该用户，请先确认目标的准确 userid。'})
        key = await self.repository.reserve_key()
        result = AddPortraitResult(subject=args.subject, portrait=PortraitRecord(key=key, preference=args.preference))
        # 回执只在节点检查点和画像写入共同提交后才会成为模型的已接受上下文。
        return ToolExecutionResult(status='succeeded', tool_result={**result.model_dump(mode='json'),
            'message': '已新增一条偏好。既有查询结果是快照，请重新搜索查看最新资料。'})

    async def delete(self, args):
        existing = await self.repository.find(self.owner_userid, args.key)
        result = DeletePortraitResult(key=args.key, deleted=existing is not None)
        return ToolExecutionResult(status='succeeded', tool_result={**result.model_dump(),
            'message': '已删除这一条偏好，请重新搜索查看最新资料。' if existing else
                       '当前助理的画像中已不存在这一条，无需重复删除。'})

    def registrations(self):
        def build(name, model, execute, description):
            async def guarded(args):
                try:
                    return await execute(args)
                except Exception as error:
                    logger.warning('画像增删预验失败：operation=%s error_type=%s', name, type(error).__name__)
                    return ToolExecutionResult(status='failed', tool_result={
                        'code': 'portrait_mutation_unavailable',
                        'message': '画像操作暂不可用，本次未提交；不能据此判断画像已新增、删除或不存在。'})
            return RegisteredTool(name, description, model,
                lambda name, raw, workspace, arguments: workspace, executor=guarded)

        return (build('add_portrait', AddPortraitArguments, self.add, PORTRAIT_TOOL_CONTRACTS[2].description),
                build('delete_portrait', DeletePortraitArguments, self.delete, PORTRAIT_TOOL_CONTRACTS[3].description))
