"""画像增删工具；基于 agent-core 契约，在 execute 内凭 current_tool_call_id 幂等落库。"""
from __future__ import annotations

import logging
from textwrap import dedent

from agent_core import (
    FeedbackOnlyHook,
    RegisteredTool,
    ToolExecutionResult,
)
from agent_core.core.tool.feedback import with_feedback
from agent_core.core.tool.registry import current_tool_call_id

from app.model.assistant_portrait import PortraitRecord, PortraitSubject
from .portrait_contract import (
    PORTRAIT_TOOL_CONTRACTS,
    AddPortraitArguments,
    AddPortraitResult,
    DeletePortraitArguments,
    DeletePortraitResult,
)

logger = logging.getLogger(__name__)


def render_add_portrait_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 偏好新增

            状态：新增失败，本次未提交
            原因及处理方向：{message}
        """).strip().format(**result)
    else:
        text = dedent("""
            ### 偏好新增

            状态：已新增一条偏好
            用户 ID：{userid}
            偏好 key：{key}
            下一步：需要核对最新资料时，请重新搜索画像。
        """).strip().format(userid=result['subject']['userid'], key=result['portrait']['key'])
    return with_feedback(execution, text)


def render_delete_portrait_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 偏好删除

            状态：删除失败，本次未提交
            原因及处理方向：{message}
        """).strip().format(**result)
    else:
        text = dedent("""
            ### 偏好删除

            状态：{state}
            偏好 key：{key}
            结果及下一步：{message}
        """).strip().format(**result, state='已删除' if result['deleted'] else '已不存在，无需删除')
    return with_feedback(execution, text)


class PortraitMutationTools:
    """私有画像增删工具；支持按 `current_tool_call_id` 幂等直写落库。"""

    def __init__(self, repository, owner_userid: str):
        self.repository = repository
        self.owner_userid = PortraitSubject(userid=owner_userid).userid

    async def add(self, args: AddPortraitArguments) -> ToolExecutionResult:
        if len(args.preference.encode('utf-8')) > 65535:
            return ToolExecutionResult(
                status='failed',
                tool_result={
                    'code': 'portrait_too_large',
                    'message': '这一条偏好过长，请精简后再新增。',
                },
            )
        if not await self.repository.target_exists(args.subject.userid):
            return ToolExecutionResult(
                status='failed',
                tool_result={
                    'code': 'portrait_target_unavailable',
                    'message': '未找到该用户，请先确认目标的准确 userid。',
                },
            )
        call_id = current_tool_call_id.get()
        if hasattr(self.repository, 'add_idempotent'):
            key = await self.repository.add_idempotent(
                owner_userid=self.owner_userid,
                subject_userid=args.subject.userid,
                preference=args.preference,
                operation_id=call_id,
            )
        else:
            key = await self.repository.reserve_key()
            if hasattr(self.repository, 'insert'):
                await self.repository.insert(
                    key=key,
                    owner_userid=self.owner_userid,
                    subject_userid=args.subject.userid,
                    preference=args.preference,
                )
        result = AddPortraitResult(
            subject=args.subject,
            portrait=PortraitRecord(key=key, preference=args.preference),
        )
        return ToolExecutionResult(
            status='succeeded',
            tool_result={
                **result.model_dump(mode='json'),
                'message': '已新增一条偏好。既有查询结果是快照，请重新搜索查看最新资料。',
            },
        )

    async def delete(self, args: DeletePortraitArguments) -> ToolExecutionResult:
        call_id = current_tool_call_id.get()
        if hasattr(self.repository, 'delete_idempotent'):
            deleted = bool(
                await self.repository.delete_idempotent(
                    owner_userid=self.owner_userid,
                    key=args.key,
                    operation_id=call_id,
                )
            )
        else:
            existing = await self.repository.find(self.owner_userid, args.key)
            deleted = existing is not None
            if deleted and hasattr(self.repository, 'delete'):
                await self.repository.delete(self.owner_userid, args.key)
        result = DeletePortraitResult(key=args.key, deleted=deleted)
        return ToolExecutionResult(
            status='succeeded',
            tool_result={
                **result.model_dump(),
                'message': (
                    '已删除这一条偏好，请重新搜索查看最新资料。'
                    if deleted
                    else '当前助理的画像中已不存在这一条，无需重复删除。'
                ),
            },
        )

    def registrations(self) -> tuple[RegisteredTool, RegisteredTool]:
        def build(name, model, execute, description, render):
            async def guarded(args):
                try:
                    return await execute(args)
                except Exception as error:
                    logger.warning('画像增删操作失败：operation=%s error_type=%s', name, type(error).__name__)
                    return ToolExecutionResult(
                        status='failed',
                        failure_mode='failed',
                        tool_result={
                            'code': 'portrait_mutation_unavailable',
                            'message': '画像操作暂不可用，本次未提交；请稍后重试，不能据此判断画像已新增、删除或不存在。',
                        },
                    )

            return RegisteredTool(
                name,
                description,
                model,
                lambda tool_name, raw, workspace, arguments: workspace,
                executor=guarded,
                hooks=(FeedbackOnlyHook(render),),
                persist_result=True,
            )

        return (
            build(
                'add_portrait',
                AddPortraitArguments,
                self.add,
                PORTRAIT_TOOL_CONTRACTS[2].description,
                render_add_portrait_feedback,
            ),
            build(
                'delete_portrait',
                DeletePortraitArguments,
                self.delete,
                PORTRAIT_TOOL_CONTRACTS[3].description,
                render_delete_portrait_feedback,
            ),
        )


__all__ = [
    'PortraitMutationTools',
    'render_add_portrait_feedback',
    'render_delete_portrait_feedback',
]
