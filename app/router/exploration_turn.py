"""探索任务注册、SSE 激活、快照与取消；HTTP 层不执行模型工具。"""
from contextlib import asynccontextmanager
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.model.question_file import UploadedFile
from app.repository.question_tree import QuestionTreeConflict, QuestionTreeNotFound
from app.router.dependency import AuthenticatedUserDependency, reject_extra_input, user_bearer
from app.router.exploration import TreePath
from app.schema.exploration_turn import TurnCreated, TurnInput, TurnSnapshot
from app.service.exploration_turn import InvalidTurnInput, ReplayUnavailable, TurnCapacity, TurnExpired

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/exploration/trees/{tree_id}/turns', tags=['exploration'],
                   dependencies=[Depends(user_bearer)])
ERRORS = {401: {'description': '中央用户登录失效'}, 404: {'description': '探索树或任务不存在'},
          409: {'description': '节点、模式、任务状态或回放游标冲突'},
          422: {'description': '请求不合法'}, 503: {'description': '依赖不可用或暂存容量已满'}}
FORM = {'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
    'type': 'object', 'additionalProperties': False,
    'required': ['mode', 'question', 'thinking_effort'], 'properties': {
        'selected_node_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$', 'description': '继续提问的任务节点；仅空树首轮省略。'},
        'mode': {'type': 'string', 'enum': ['main', 'branch'], 'description': '创建模式：main 为推进，生成主干节点；branch 为探索，生成分支节点。当前主干末尾允许两者，历史主干或分支节点仅允许探索；空树首轮只能推进。'},
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 20000, 'description': '本轮用户问题，不能为空白。'},
        'thinking_effort': {'type': 'string', 'enum': ['low', 'medium', 'xhigh'], 'description': '低、中、高三档推理强度。'},
        'supersedes_turn_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$',
                               'description': '改变方向时显式指定同树尚未结束的旧任务 ID；省略时为普通新任务。'},
        'files': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'format': 'binary'},
                  'description': '重复 files 字段上传 PDF，每份 10 MiB，合计 20 MiB。'},
    }}}}}}


@asynccontextmanager
async def errors():
    try:
        yield
    except HTTPException:
        raise
    except InvalidTurnInput:
        raise HTTPException(422, '替代任务 PDF 不合法', headers={'Cache-Control': 'no-store'}) from None
    except QuestionTreeNotFound:
        raise HTTPException(404, '问答树或任务不存在', headers={'Cache-Control': 'no-store'}) from None
    except TurnExpired:
        raise HTTPException(410, '任务超过首次激活期限，请重新创建', headers={'Cache-Control': 'no-store'}) from None
    except ReplayUnavailable:
        raise HTTPException(409, '游标不可回放，请先获取任务快照', headers={'Cache-Control': 'no-store'}) from None
    except QuestionTreeConflict:
        raise HTTPException(409, '节点、模式或任务状态冲突，请重新打开问答树确认', headers={'Cache-Control': 'no-store'}) from None
    except Exception as error:
        logger.warning('探索任务请求失败：error_type=%s', type(error).__name__)
        raise HTTPException(503, '问答任务暂不可用', headers={'Cache-Control': 'no-store'}) from None


def service(request):
    return request.app.state.exploration_turn_service


@router.post('', status_code=201, response_model=TurnCreated, summary='注册待激活探索任务',
             responses={**ERRORS, 413: {'description': '上传数量或字节超限'}}, openapi_extra=FORM)
async def create_turn(tree_id: TreePath, request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request, allow_body=True)
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'multipart/form-data':
        raise HTTPException(422, '请使用 multipart/form-data', headers={'Cache-Control': 'no-store'})
    # 手工解析以限制 multipart 数量，同时在读取上传前完成中央身份校验。
    try:
        async with request.form(max_files=10, max_fields=5, max_part_size=80000) as form:
            if set(form) - {'selected_node_id', 'mode', 'question', 'thinking_effort', 'supersedes_turn_id', 'files'}:
                raise ValueError()
            fields = {}
            for key in ('selected_node_id', 'mode', 'question', 'thinking_effort', 'supersedes_turn_id'):
                values = form.getlist(key)
                if len(values) > 1 or any(not isinstance(value, str) for value in values):
                    raise ValueError()
                if values:
                    fields[key] = values[0]
            body = TurnInput.model_validate(fields)
            uploads, total = [], 0
            for file in form.getlist('files'):
                if not isinstance(file, UploadFile):
                    raise ValueError()
                filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
                if not filename.lower().endswith('.pdf') or len(filename) > 512:
                    raise ValueError()
                chunks, size = [], 0
                while chunk := await file.read(65536):
                    size += len(chunk)
                    total += len(chunk)
                    if size > 10 * 1024 * 1024 or total > 20 * 1024 * 1024:
                        raise HTTPException(413, 'PDF 每份最多 10 MiB、合计最多 20 MiB', headers={'Cache-Control': 'no-store'})
                    chunks.append(chunk)
                uploads.append(UploadedFile(type='images', content=b''.join(chunks)))
            async with errors():
                result = await service(request).register(tree_id, user, body, uploads)
    except (ValidationError, ValueError):
        raise HTTPException(422, '请求参数或 PDF 文件不合法', headers={'Cache-Control': 'no-store'}) from None
    except StarletteHTTPException as error:
        if error.status_code == 400:
            # multipart 解析错误不回显文件名、字段值或内部解析细节。
            raise HTTPException(413, '上传数量超限或 multipart 格式不合法', headers={'Cache-Control': 'no-store'}) from None
        raise
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.get('/{turn_id}/events', response_class=StreamingResponse, summary='订阅并激活探索任务',
            responses={**ERRORS, 410: {'description': '首次激活超时'},
                200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
async def events(tree_id: TreePath, turn_id: TreePath, request: Request, user: AuthenticatedUserDependency,
                 last_event_id: Annotated[str | None, Header(alias='Last-Event-ID')] = None):
    await reject_extra_input(request)
    if last_event_id is not None and (not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 20):
        raise HTTPException(422, 'Last-Event-ID 必须是非负整数', headers={'Cache-Control': 'no-store'})
    async with errors():
        stream = await service(request).subscribe(tree_id, turn_id, user, int(last_event_id or 0))
    async def iterate():
        try:
            async for event in stream:
                if event is None:
                    yield ': heartbeat\n\n'
                else:
                    yield f"id: {event['sequence']}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        except (ReplayUnavailable, QuestionTreeNotFound) as error:
            data = {'turn_id': turn_id, 'code': 'replay_required' if isinstance(error, ReplayUnavailable) else 'turn_unavailable',
                    'message': '请重新获取任务快照或打开问答树', 'retryable': isinstance(error, ReplayUnavailable)}
            yield 'event: error\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
        finally:
            # 关闭 HTTP 订阅不取消后台执行；首次订阅已经激活，重连仅回放。
            await stream.aclose()
    return StreamingResponse(iterate(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


@router.get('/{turn_id}', response_model=TurnSnapshot, summary='获取探索任务展示快照', responses=ERRORS)
async def snapshot(tree_id: TreePath, turn_id: TreePath, request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    async with errors():
        result = await service(request).snapshot(tree_id, turn_id, user.userid)
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.post('/{turn_id}/cancel', response_model=TurnSnapshot, summary='停止探索任务', responses=ERRORS)
async def cancel(tree_id: TreePath, turn_id: TreePath, request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    async with errors():
        result = await service(request).cancel(tree_id, turn_id, user.userid)
    response.headers['Cache-Control'] = 'no-store'
    return result
