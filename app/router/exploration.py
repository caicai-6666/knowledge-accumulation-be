"""探索模式问答树创建、列表、打开、改名与删除。"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from app.model.question_tree import QuestionTreeListItem, TreeID
from app.repository.question_tree import QuestionTreeConflict, QuestionTreeNotFound
from app.router.dependency import AuthenticatedUserDependency, reject_extra_input, user_bearer
from app.schema.auth import AuthErrorResponse
from app.schema.exploration import CreateTreeRequest, ExplorationTreeListItem, ExplorationTreeResponse, RenameTreeRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/exploration', tags=['exploration'])
TreePath = Annotated[TreeID, Path(description='问答树 ID，32 位小写十六进制字符串。')]


@router.get('/trees', response_model=list[ExplorationTreeListItem], summary='获取探索问答树列表',
            dependencies=[Depends(user_bearer)], responses={
                401: {'description': '用户 token 无效或中央登录已失效', 'model': AuthErrorResponse},
                422: {'description': '请求参数不合法', 'model': AuthErrorResponse},
                503: {'description': '认证或问答树存储暂不可用', 'model': AuthErrorResponse},
            })
async def list_trees(request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    repository = getattr(request.app.state, 'question_tree_repository', None)
    try:
        if repository is None:
            raise RuntimeError('问答树仓库未装配')
        # 身份只能来自中央认证结果；客户端不能指定 userid 或切换到 assistant 类型。
        trees = await repository.list_exploration_trees(user.userid)
        turns = getattr(request.app.state, 'exploration_turn_service', None)
        if turns is None:
            raise RuntimeError('探索任务服务未装配')
        running = await turns.running_tree_ids(user.userid)
        result = [ExplorationTreeListItem(id=tree.id, name=tree.name,
                    has_running_task=tree.id in running) for tree in trees]
    except Exception as error:
        logger.warning('探索问答树列表查询失败：error_type=%s', type(error).__name__)
        raise HTTPException(status_code=503, detail='问答树列表暂不可用',
                            headers={'Cache-Control': 'no-store'}) from None
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.post('/trees', status_code=201, response_model=QuestionTreeListItem, summary='创建探索问答树',
             dependencies=[Depends(user_bearer)], responses={
                 401: {'description': '用户 token 无效或中央登录已失效', 'model': AuthErrorResponse},
                 422: {'description': '名称或请求参数不合法', 'model': AuthErrorResponse},
                 503: {'description': '认证或问答树存储暂不可用', 'model': AuthErrorResponse},
             })
async def create_tree(body: CreateTreeRequest, request: Request, response: Response,
                      user: AuthenticatedUserDependency):
    await reject_extra_input(request, allow_body=True)
    repository = getattr(request.app.state, 'question_tree_repository', None)
    try:
        if repository is None:
            raise RuntimeError('问答树仓库未装配')
        # ID 由仓库生成，归属来自中央身份；客户端不能创建他人或助理模式树。
        tree = await repository.create_tree(user.userid, body.name, kind='exploration')
        result = QuestionTreeListItem(id=tree.id, name=tree.name)
    except Exception as error:
        logger.warning('探索问答树创建失败：error_type=%s', type(error).__name__)
        raise HTTPException(503, '问答树创建暂不可用', headers={'Cache-Control': 'no-store'}) from None
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.patch('/trees/{tree_id}/name', response_model=QuestionTreeListItem, summary='修改探索问答树名称',
              dependencies=[Depends(user_bearer)], responses={
                  401: {'description': '用户 token 无效或中央登录已失效', 'model': AuthErrorResponse},
                  404: {'description': '探索树不存在或不属于当前用户', 'model': AuthErrorResponse},
                  422: {'description': '树 ID、名称或请求参数不合法', 'model': AuthErrorResponse},
                  503: {'description': '认证或问答树存储暂不可用', 'model': AuthErrorResponse},
              })
async def rename_tree(tree_id: TreePath, body: RenameTreeRequest, request: Request,
                      response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request, allow_body=True)
    repository = getattr(request.app.state, 'question_tree_repository', None)
    try:
        if repository is None:
            raise RuntimeError('问答树仓库未装配')
        tree = await repository.rename_exploration_tree(tree_id, user.userid, body.name)
    except QuestionTreeNotFound:
        raise HTTPException(status_code=404, detail='问答树不存在',
                            headers={'Cache-Control': 'no-store'}) from None
    except Exception as error:
        logger.warning('探索问答树改名失败：error_type=%s', type(error).__name__)
        raise HTTPException(status_code=503, detail='问答树改名暂不可用',
                            headers={'Cache-Control': 'no-store'}) from None
    response.headers['Cache-Control'] = 'no-store'
    return tree


@router.post('/trees/{tree_id}/open', response_model=ExplorationTreeResponse, summary='打开探索问答树',
             dependencies=[Depends(user_bearer)], responses={
                 401: {'description': '用户 token 无效或中央登录已失效', 'model': AuthErrorResponse},
                 404: {'description': '探索树不存在或不属于当前用户', 'model': AuthErrorResponse},
                 422: {'description': '树 ID 或请求参数不合法', 'model': AuthErrorResponse},
                 503: {'description': '认证或问答树存储暂不可用，或树数据不完整', 'model': AuthErrorResponse},
             })
async def open_tree(tree_id: TreePath, request: Request, response: Response, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    service = getattr(request.app.state, 'exploration_tree_service', None)
    try:
        if service is None:
            raise RuntimeError('探索树服务未装配')
        tree = await service.open_tree(tree_id, user.userid)
    except QuestionTreeNotFound:
        raise HTTPException(status_code=404, detail='问答树不存在',
                            headers={'Cache-Control': 'no-store'}) from None
    except Exception as error:
        logger.warning('探索问答树打开失败：error_type=%s', type(error).__name__)
        raise HTTPException(status_code=503, detail='问答树打开暂不可用',
                            headers={'Cache-Control': 'no-store'}) from None
    response.headers['Cache-Control'] = 'no-store'
    return tree


@router.delete('/trees/{tree_id}', status_code=204, summary='删除探索问答树及附件',
               dependencies=[Depends(user_bearer)], responses={
                   401: {'description': '用户 token 无效或中央登录已失效', 'model': AuthErrorResponse},
                   404: {'description': '探索树不存在或不属于当前用户', 'model': AuthErrorResponse},
                   409: {'description': '附件跨树引用或类型冲突', 'model': AuthErrorResponse},
                   422: {'description': '树 ID 或请求参数不合法', 'model': AuthErrorResponse},
                   503: {'description': '认证、数据库或文件清理失败，可重试相同删除请求', 'model': AuthErrorResponse},
               })
async def delete_tree(tree_id: TreePath, request: Request, user: AuthenticatedUserDependency):
    await reject_extra_input(request)
    service = getattr(request.app.state, 'exploration_tree_service', None)
    try:
        if service is None:
            raise RuntimeError('探索树服务未装配')
        await service.delete_tree(tree_id, user.userid)
    except QuestionTreeNotFound:
        raise HTTPException(status_code=404, detail='问答树不存在',
                            headers={'Cache-Control': 'no-store'}) from None
    except QuestionTreeConflict:
        raise HTTPException(status_code=409, detail='附件登记存在冲突，暂不能删除',
                            headers={'Cache-Control': 'no-store'}) from None
    except Exception as error:
        logger.warning('探索问答树删除失败：error_type=%s', type(error).__name__)
        raise HTTPException(status_code=503, detail='问答树删除尚未完成，请重试',
                            headers={'Cache-Control': 'no-store'}) from None
    return Response(status_code=204, headers={'Cache-Control': 'no-store'})
