"""应用组合根：在生命周期内维护中间件平台会话。"""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse

from app.auth.developer import DeveloperAuthentication
from app.auth.dingtalk import DingTalkAuthentication
from app.auth.error import AuthUnavailable, InvalidCredentials
from app.config.avatar import AvatarSettings, load_avatar_settings
from app.infrastructure.avatar import AvatarStore
from app.infrastructure.registry_file import RegistryFileClient
from app.config.registry_file import load_registry_file_settings
from app.infrastructure.dingtalk import DingTalkClient
from app.router.auth import router as auth_router
from app.router.resource import router as resource_router
from app.router.platform import router as platform_router
from app.router.exploration import router as exploration_router
from app.router.exploration_turn import router as exploration_turn_router
from app.service.exploration_turn import ExplorationTurnService
from app.repository.node_persistence import MySQLNodeRepository
from app.service.question_file import QuestionFileService
from app.agent.exploration.factory import build_service
from app.config.mllm import load_mllm_settings
from app.service.auth import AuthService
from app.service.employee_onboarding import EmployeeOnboardingService
from app.service.middleware_user_auth import MiddlewareUserAuthService
from app.service.platform_directory import PlatformDirectoryService
from app.service.platform_query import PlatformQueryService
from app.config.auth import AuthSettings, load_auth_settings
from app.config.middleware import MiddlewareSettings, load_middleware_settings
from app.config.middleware_user import MiddlewareUserSyncSettings, load_middleware_user_sync_settings
from app.infrastructure.middleware import MiddlewareClient
from app.repository.employee import MySQLEmployeeRepository
from app.repository.question_tree import MySQLQuestionTreeRepository
from app.service.middleware_session import MiddlewareSessionService
from app.service.middleware_user import MiddlewareUserSyncService
from app.service.exploration_tree import ExplorationTreeService
from app.infrastructure.upload_file import UploadFileStore
from app.config.embedding import load_optional_embedding_settings
from app.config.memory import load_memory_embedding_settings
from app.service.embedding import EmbeddingService
from app.service.file_retrieval import FileRetrievalService
from app.service.registry_file_access import RegistryFileAccessService
from app.config.file_retrieval import load_file_retrieval_settings
from app.service.memory_embedding import MemoryEmbeddingService
from app.service.memory_retrieval import AncestorMemoryRetrieval
from app.config.assistant_runtime import AssistantRuntimeSettings, load_assistant_runtime_settings
from app.config.assistant_queue import load_assistant_queue_settings
from app.config.dingtalk_robot import load_dingtalk_robot_settings
from app.infrastructure.assistant_queue import AssistantQueueClient
from app.infrastructure.dingtalk_robot import DingTalkRobotClient
from app.infrastructure.dingtalk_stream import DingTalkRobotStream
from app.repository.robot_inbox import RobotInboxRepository
from app.service.robot_inbound import RobotInboundService
from app.repository.assistant_runtime import MySQLAssistantRepository
from app.service.assistant_queue import AssistantQueueService
from app.service.assistant_delivery import AssistantDeliveryService
from app.service.assistant_consumer import AssistantConsumer
from app.service.assistant_reminder import AssistantReminderScheduler
from app.service.assistant_heartbeat import AssistantHeartbeatScheduler
from app.agent.assistant.factory import build_service as build_assistant_service

API_PREFIX = '/knowledge-accumulation/api'


def create_app(
    settings: MiddlewareSettings | None = None,
    *, middleware_client: MiddlewareClient | None = None,
    user_sync_settings: MiddlewareUserSyncSettings | None = None,
    auth_settings: AuthSettings | None = None,
    employees: MySQLEmployeeRepository | None = None,
    user_sync_client: MiddlewareClient | None = None,
    dingtalk_client: DingTalkClient | None = None,
    avatar_settings: AvatarSettings | None = None,
    question_trees: MySQLQuestionTreeRepository | None = None,
    upload_files: UploadFileStore | None = None,
    question_agent_factory=None,
    memory_embedding_settings_loader=load_optional_embedding_settings,
    registry_file_client: RegistryFileClient | None = None,
    assistant_runtime_settings: AssistantRuntimeSettings | None = None,
    assistant_robot_client=None,
    assistant_robot_settings=None,
    assistant_stream_factory=DingTalkRobotStream,
    assistant_agent_factory=None,
) -> FastAPI:
    # 钉钉旧接口在 URL 查询参数中携带应用 token；关闭默认明细日志，防止凭据进入日志。
    for logger_name in ('httpx', 'httpcore', 'uvicorn.access'):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        middleware = settings if settings is not None else load_middleware_settings()
        sync = user_sync_settings if user_sync_settings is not None else load_middleware_user_sync_settings()
        auth = auth_settings if auth_settings is not None else load_auth_settings()
        assistant_settings=assistant_runtime_settings or load_assistant_runtime_settings()
        if assistant_settings.enabled and not middleware.enabled:
            raise ValueError('启用助理消费者需要配置本平台中间件会话')
        robot_settings=None
        assistant_mllm=None
        if assistant_settings.enabled:
            robot_settings=assistant_robot_settings or load_dingtalk_robot_settings()
            robot_settings.require_credentials()
            if assistant_agent_factory is None:assistant_mllm=load_mllm_settings()
        if middleware.enabled and middleware.platform_code != 'knowledge-accumulation':
            raise ValueError('用户登录和总表同步必须使用 knowledge-accumulation 平台身份')
        # 登录是核心能力；中间件配置存在时直接装配，缺失凭据在任何后台 HTTP 任务前本地失败。
        if middleware.enabled:
            if auth.mode == 'development':
                auth.require_developer_credentials()
            else:
                auth.require_dingtalk_credentials()
        async with AsyncExitStack() as stack:
            # 仓库按首次树请求连接 MySQL，启动只负责装配与关闭，不创建表。
            trees = question_trees if question_trees is not None else MySQLQuestionTreeRepository()
            stack.push_async_callback(trees.close)
            application.state.question_tree_repository = trees
            application.state.assistant_consumer = None
            application.state.assistant_reminder_scheduler = None
            application.state.assistant_heartbeat_scheduler = None
            application.state.assistant_robot_inbound = None
            application.state.assistant_robot_stream = None
            application.state.memory_embedding_service = None
            embedding_client = None
            embedding_settings = memory_embedding_settings_loader()
            if embedding_settings is not None:
                embedding_client = EmbeddingService(embedding_settings)
                stack.push_async_callback(embedding_client.close)
                memory_embeddings = MemoryEmbeddingService(trees, embedding_client, load_memory_embedding_settings())
                application.state.memory_embedding_service = memory_embeddings
                # 注册在仓库/模型客户端之后：关闭时先取消并等待后台扫描，再释放依赖。
                stack.push_async_callback(memory_embeddings.close)
                await memory_embeddings.start()
            memory_retrieval = AncestorMemoryRetrieval(trees, embedding_client)
            stack.push_async_callback(memory_retrieval.close)
            file_retrieval = FileRetrievalService(embedding_client, settings=load_file_retrieval_settings())
            stack.push_async_callback(file_retrieval.close)
            application.state.file_retrieval_service = file_retrieval
            registry_files = registry_file_client if registry_file_client is not None else RegistryFileClient(load_registry_file_settings())
            stack.push_async_callback(registry_files.close)
            application.state.registry_file_client = registry_files
            application.state.registry_file_access = None
            files = upload_files if upload_files is not None else UploadFileStore()
            nodes = MySQLNodeRepository(trees)
            file_service = QuestionFileService(nodes, files)
            application.state.question_file_service = file_service
            exploration = ExplorationTreeService(trees, upload_files=files)
            application.state.exploration_tree_service = exploration
            stack.push_async_callback(exploration.close)
            def make_agent():
                return build_service(nodes, load_mllm_settings(), file_service=file_service, memory_backend=memory_retrieval,
                    platform_directory=application.state.platform_directory_service,file_retrieval=file_retrieval,registry_files=registry_files,
                    registry_file_access=application.state.registry_file_access)
            # 模型配置和客户端只在首次激活时装配；关闭先停止执行，再关闭树仓库。
            turns = ExplorationTurnService(trees, exploration, question_agent_factory or make_agent)
            exploration.turns = turns
            application.state.exploration_turn_service = turns
            service = MiddlewareSessionService(middleware, client=middleware_client)
            stack.push_async_callback(service.close)
            application.state.middleware_session_service = service
            application.state.middleware_user_sync_service = None
            application.state.auth_service = None
            application.state.avatar_store = None
            application.state.platform_query_service = None
            application.state.platform_directory_service = None
            await service.start()
            if middleware.enabled:
                # 总表快照与显式登录共用唯一维护写锁，避免旧快照删除刚创建的中央登录。
                maintenance_lock = asyncio.Lock()
                repository = employees if employees is not None else MySQLEmployeeRepository()
                stack.push_async_callback(repository.close)
                client = user_sync_client if user_sync_client is not None else MiddlewareClient(
                    str(middleware.base_url), timeout_seconds=middleware.request_timeout_seconds,
                )
                stack.push_async_callback(client.close)
                application.state.employee_repository = repository
                directory = PlatformDirectoryService(service, client, timeout_seconds=auth.middleware_timeout_seconds)
                application.state.platform_directory_service = directory
                application.state.platform_query_service = PlatformQueryService(
                    service, client, repository, timeout_seconds=auth.operation_timeout_seconds)
                registration = MiddlewareUserSyncService(
                    sync, repository, service, client, maintenance_lock=maintenance_lock,
                    simulated_user=auth.simulated_user(),
                )
                application.state.middleware_user_sync_service = registration
                # 两种登录模式共用一个 Store；构造不创建目录，读取接口不触发下载或写入。
                avatars = AvatarStore(avatar_settings if avatar_settings is not None else load_avatar_settings())
                stack.push_async_callback(avatars.close)
                application.state.avatar_store = avatars
                central = MiddlewareUserAuthService(service, client, repository,
                    maintenance_lock=maintenance_lock, registration=registration, timeout_seconds=auth.middleware_timeout_seconds)
                application.state.registry_file_access = RegistryFileAccessService(central, file_retrieval)
                if auth.mode == 'development':
                    backend = DeveloperAuthentication(auth, central=central, employees=repository)
                else:
                    # 生产模式始终准备登录依赖；开发模式不构造钉钉客户端。
                    auth.require_dingtalk_credentials()
                    dingtalk = dingtalk_client if dingtalk_client is not None else DingTalkClient(auth)
                    stack.push_async_callback(dingtalk.close)
                    onboarding = EmployeeOnboardingService(dingtalk, repository, avatars, directory,
                        own_platform_code=middleware.platform_code, timeout_seconds=auth.operation_timeout_seconds)
                    backend = DingTalkAuthentication(dingtalk, repository, onboarding, central)
                application.state.auth_service = AuthService(auth, backend)
                # 先取消启动注册任务，再关闭登录资源；显式首次登记与启动任务共享相同协调器和维护锁。
                stack.push_async_callback(registration.close)
                await registration.start()
                if assistant_settings.enabled:
                    queue_client=AssistantQueueClient(str(middleware.base_url),settings=load_assistant_queue_settings())
                    stack.push_async_callback(queue_client.close)
                    robot=assistant_robot_client or DingTalkRobotClient(robot_settings)
                    stack.push_async_callback(robot.close)
                    queue=AssistantQueueService(service,queue_client,platform_code=middleware.platform_code)
                    stack.push_async_callback(queue.close)
                    assistant_repository=MySQLAssistantRepository(trees)
                    delivery=AssistantDeliveryService(assistant_repository,queue,robot,
                        publication_retry_window_seconds=assistant_settings.publication_retry_window_seconds)
                    def make_assistant(**kwargs):
                        return build_assistant_service(settings=assistant_mllm,memory_backend=memory_retrieval,
                            platform_directory=directory,file_retrieval=file_retrieval,**kwargs)
                    consumer=AssistantConsumer(assistant_settings,assistant_repository,queue,delivery,repository,files,
                        assistant_agent_factory or make_assistant)
                    application.state.assistant_consumer=consumer
                    stack.push_async_callback(consumer.close)
                    await consumer.start()
                    scheduler=AssistantReminderScheduler(assistant_settings,assistant_repository.reminders,delivery)
                    application.state.assistant_reminder_scheduler=scheduler
                    stack.push_async_callback(scheduler.close)
                    await scheduler.start()
                    heartbeat=AssistantHeartbeatScheduler(assistant_settings,assistant_repository,repository,queue)
                    application.state.assistant_heartbeat_scheduler=heartbeat
                    stack.push_async_callback(heartbeat.close)
                    await heartbeat.start()
                    inbound=RobotInboundService(assistant_settings,RobotInboxRepository(trees),repository,
                        queue,robot,files)
                    application.state.assistant_robot_inbound=inbound
                    stack.push_async_callback(inbound.close)
                    await inbound.start()
                    stream=assistant_stream_factory(robot_settings,inbound.receive,client=robot)
                    application.state.assistant_robot_stream=stream
                    # 停机先停止入站回调与待发布任务，再关闭消费者、队列和 HTTP 客户端。
                    stack.push_async_callback(stream.close)
                    await stream.start()
            # 问答工具依赖平台目录HTTP：退出时先取消并等待问答，再关闭平台客户端和会话。
            stack.push_async_callback(turns.close)
            await turns.start()
            yield

    application = FastAPI(title="企业知识沉淀客户后端", lifespan=lifespan)
    application.include_router(auth_router, prefix=API_PREFIX)
    application.include_router(resource_router, prefix=API_PREFIX)
    application.include_router(platform_router, prefix=API_PREFIX)
    application.include_router(exploration_router, prefix=API_PREFIX)
    application.include_router(exploration_turn_router, prefix=API_PREFIX)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # 不返回 loc/msg/input，避免非法字段名、code 或 token 被默认 422 回显。
        return JSONResponse(status_code=422, content={'detail':'请求参数不合法'}, headers={'Cache-Control':'no-store'})

    @application.exception_handler(ResponseValidationError)
    async def response_error(request, error):
        return JSONResponse(status_code=503, content={'detail':'用户认证响应暂不可用'}, headers={'Cache-Control':'no-store'})

    async def auth_error(request, error):
        status = 401 if isinstance(error, InvalidCredentials) else 503
        headers = {'Cache-Control':'no-store'}
        if status == 401:
            headers['WWW-Authenticate'] = 'Bearer'
        return JSONResponse(status_code=status, content={'detail':str(error)}, headers=headers)

    for error_type in (AuthUnavailable, InvalidCredentials):
        application.add_exception_handler(error_type, auth_error)
    return application


app = create_app()


if __name__ == "__main__":
    from pathlib import Path
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # 本地开发仅监视应用源码，避免运行数据写入触发重启；生产由独立CLI启动。
    uvicorn.run("app.main:app", host="127.0.0.1", port=24000,
                reload=True, reload_dirs=[str(Path(__file__).resolve().parent)])
