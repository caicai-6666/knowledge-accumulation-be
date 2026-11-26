"""应用组合根：维护中间件平台会话、身份服务及基于 agent-core 的探索与个人助理服务。"""

import os

# 路由前缀在模块首次导入时确定；必须早于 app.config.api 及其间接导入。
# 直接/模块执行是本地开发入口，ASGI 导入继续使用部署环境指定的前缀。
if __name__ == '__main__':
    os.environ['APP_PATH_PREFIX'] = '/dev'

# 支持 IDE 直接运行 app/main.py，与 python -m app.main 使用相同导入路径。
if __name__ == '__main__' and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager
import logging
from typing import Any

from agent_core import SchedulerSettings
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse

from app.agent.assistant.message_rendering import DingTalkMessageRenderer
from app.agent.assistant.message_rendering.hook import AssistantMessageRenderingHook
from app.auth.developer import DeveloperAuthentication
from app.auth.dingtalk import DingTalkAuthentication
from app.auth.error import AuthUnavailable, InvalidCredentials
from app.config.api import API_PREFIX
from app.config.assistant_queue import load_assistant_queue_settings
from app.config.assistant_runtime import AssistantRuntimeSettings, load_assistant_runtime_settings
from app.config.auth import AuthSettings, load_auth_settings
from app.config.avatar import AvatarSettings, load_avatar_settings
from app.config.dingtalk_robot import load_dingtalk_robot_settings
from app.config.embedding import load_optional_embedding_settings
from app.config.file_retrieval import load_file_retrieval_settings
from app.config.mcp import MCPSettings, load_mcp_settings
from app.config.middleware import MiddlewareSettings, load_middleware_settings
from app.config.middleware_user import MiddlewareUserSyncSettings, load_middleware_user_sync_settings
from app.config.mllm import load_mllm_settings
from app.config.platform_a2a import PlatformA2ASettings, load_platform_a2a_settings
from app.config.registry_file import load_registry_file_settings
from app.infrastructure.assistant_queue import AssistantQueueClient
from app.infrastructure.avatar import AvatarStore
from app.infrastructure.dingtalk import DingTalkClient
from app.infrastructure.dingtalk_calendar import DingTalkCalendarClient
from app.infrastructure.dingtalk_robot import DingTalkRobotClient
from app.infrastructure.dingtalk_stream import DingTalkRobotStream
from app.infrastructure.mcp_client import MCPClientManager
from app.infrastructure.middleware import MiddlewareClient
from app.infrastructure.platform_a2a import PlatformA2AClient
from app.infrastructure.registry_file import RegistryFileClient
from app.infrastructure.upload_file import UploadFileStore
from app.repository.assistant_file_note import AssistantFileNoteRepository
from app.repository.assistant_runtime import MySQLAssistantRepository
from app.repository.employee import MySQLEmployeeRepository
from app.repository.exploration_tree import ExplorationTreeRepository
from app.repository.mysql_base import MySQLRepositoryBase
from app.repository.platform_a2a import PlatformA2ARepository
from app.repository.robot_inbox import RobotInboxRepository
from app.repository.uploaded_file import UploadedFileRepository
from app.router.auth import router as auth_router
from app.router.employee_avatar import router as employee_avatar_router
from app.router.exploration import router as exploration_router
from app.router.exploration_turn import router as exploration_turn_router
from app.router.platform import router as platform_router
from app.router.resource import router as resource_router
from app.service.assistant_consumer import AssistantConsumer
from app.service.assistant_delivery import AssistantDeliveryService
from app.service.assistant_file_delivery import AssistantFileDeliveryService
from app.service.assistant_queue import AssistantQueueService
from app.service.assistant_runtime import AssistantRuntimeService, AssistantSchedulerRunner
from app.service.auth import AuthService
from app.service.calendar import CalendarService
from app.service.embedding import EmbeddingService
from app.service.employee_avatar import EmployeeAvatarService
from app.service.employee_onboarding import EmployeeOnboardingService
from app.service.exploration_attachment import prepare_uploads_async
from app.service.exploration_runtime import ExplorationRuntimeService
from app.service.exploration_tree import ExplorationTreeService
from app.service.exploration_turn import ExplorationTurnService
from app.service.file_retrieval import FileRetrievalService
from app.service.middleware_session import MiddlewareSessionService
from app.service.middleware_user import MiddlewareUserSyncService
from app.service.middleware_user_auth import MiddlewareUserAuthService
from app.service.platform_a2a import PlatformA2AService
from app.service.platform_directory import PlatformDirectoryService
from app.service.platform_query import PlatformQueryService
from app.service.registry_file_access import RegistryFileAccessService
from app.service.robot_inbound import RobotInboundService
from app.service.robot_reply_file import RobotReplyFileResolver
from app.service.uploaded_file import FileVisibilityService, UploadedFileRegistrationService


def create_app(
    settings: MiddlewareSettings | None = None,
    *,
    middleware_client: MiddlewareClient | None = None,
    user_sync_settings: MiddlewareUserSyncSettings | None = None,
    auth_settings: AuthSettings | None = None,
    employees: MySQLEmployeeRepository | None = None,
    user_sync_client: MiddlewareClient | None = None,
    dingtalk_client: DingTalkClient | None = None,
    avatar_settings: AvatarSettings | None = None,
    upload_files: UploadFileStore | None = None,
    memory_embedding_settings_loader=load_optional_embedding_settings,
    registry_file_client: RegistryFileClient | None = None,
    mcp_settings: MCPSettings | None = None,
    mcp_client: MCPClientManager | None = None,
    exploration_repository: ExplorationTreeRepository | None = None,
    exploration_runtime: ExplorationRuntimeService | None = None,
    exploration_models: Any = None,
    exploration_extra_tools: Sequence[Any] = (),
    exploration_attachment_converter=None,
    assistant_runtime_settings: AssistantRuntimeSettings | None = None,
    platform_a2a_settings: PlatformA2ASettings | None = None,
    assistant_robot_client=None,
    assistant_robot_settings=None,
    assistant_stream_factory=DingTalkRobotStream,
    assistant_finish_hooks=None,
    mysql_base: MySQLRepositoryBase | None = None,
) -> FastAPI:
    # 钉钉旧接口在 URL 查询参数中携带应用 token；关闭默认明细日志，防止凭据进入日志。
    for logger_name in ('httpx', 'httpcore', 'uvicorn.access'):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        middleware = settings if settings is not None else load_middleware_settings()
        sync = user_sync_settings if user_sync_settings is not None else load_middleware_user_sync_settings()
        auth = auth_settings if auth_settings is not None else load_auth_settings()
        mcp_cfg = mcp_settings if mcp_settings is not None else load_mcp_settings()
        assistant_settings = (
            assistant_runtime_settings
            if assistant_runtime_settings is not None
            else load_assistant_runtime_settings()
        )
        a2a_settings = (
            platform_a2a_settings
            if platform_a2a_settings is not None
            else load_platform_a2a_settings()
        )

        if assistant_settings.enabled and not middleware.enabled:
            raise ValueError('启用助理消费者需要配置本平台中间件会话')
        if a2a_settings.enabled and not middleware.enabled:
            raise ValueError('启用平台 A2A 需要本平台中间件会话')
        robot_settings = None
        assistant_mllm = None
        if assistant_settings.enabled:
            robot_settings = assistant_robot_settings or load_dingtalk_robot_settings()
            robot_settings.require_credentials()
            if assistant_finish_hooks is None:
                assistant_mllm = load_mllm_settings()

        if middleware.enabled and middleware.platform_code != 'knowledge-accumulation':
            raise ValueError('用户登录和总表同步必须使用 knowledge-accumulation 平台身份')
        # 登录是核心能力；中间件配置存在时直接装配，缺失凭据在任何后台 HTTP 任务前本地失败。
        if middleware.enabled:
            if auth.mode == 'development':
                auth.require_developer_credentials()
            else:
                auth.require_dingtalk_credentials()

        async with AsyncExitStack() as stack:
            db_base = mysql_base if mysql_base is not None else MySQLRepositoryBase()
            stack.push_async_callback(db_base.close)
            application.state.mysql_base = db_base

            embedding_client = None
            embedding_settings = memory_embedding_settings_loader()
            if embedding_settings is not None:
                embedding_client = EmbeddingService(embedding_settings)
                stack.push_async_callback(embedding_client.close)

            file_retrieval = FileRetrievalService(embedding_client, settings=load_file_retrieval_settings())
            stack.push_async_callback(file_retrieval.close)
            application.state.file_retrieval_service = file_retrieval

            service = MiddlewareSessionService(middleware, client=middleware_client)
            stack.push_async_callback(service.close)
            application.state.middleware_session_service = service

            registry_files = (
                registry_file_client
                if registry_file_client is not None
                else RegistryFileClient(load_registry_file_settings(), sessions=service)
            )
            stack.push_async_callback(registry_files.close)
            application.state.registry_file_client = registry_files
            application.state.registry_file_access = None

            files = upload_files if upload_files is not None else UploadFileStore()
            application.state.upload_file_store = files

            uploaded_file_repository = UploadedFileRepository(db_base)
            file_registration = UploadedFileRegistrationService(uploaded_file_repository, files)
            file_visibility = FileVisibilityService(uploaded_file_repository)
            application.state.uploaded_file_registration = file_registration
            application.state.file_visibility_service = file_visibility

            # MCP 客户端（用于探索/助理外部工具与附件转 PDF）
            active_mcp: MCPClientManager | None = mcp_client
            if active_mcp is None and mcp_cfg.enabled:
                active_mcp = MCPClientManager(mcp_cfg)
                stack.push_async_callback(active_mcp.aclose)
            application.state.mcp_client = active_mcp

            # 基于 agent-core 的探索存储、运行时、树服务与任务服务
            repo = exploration_repository if exploration_repository is not None else ExplorationTreeRepository.from_mysql()
            stack.push_async_callback(repo.close)
            application.state.exploration_tree_repository = repo

            runtime = (
                exploration_runtime
                if exploration_runtime is not None
                else ExplorationRuntimeService(
                    repo,
                    models=exploration_models,
                    upload_files=files,
                    mcp_client=active_mcp,
                    file_retrieval_service=file_retrieval,
                    registry_file_client=registry_files,
                    extra_tools=exploration_extra_tools,
                )
            )
            stack.push_async_callback(runtime.close)
            application.state.exploration_runtime = runtime

            exploration = ExplorationTreeService(repo, runtime, upload_files=files)
            stack.push_async_callback(exploration.close)
            application.state.exploration_tree_service = exploration

            converter = exploration_attachment_converter
            if converter is None:
                async def _default_convert(uploads):
                    return await prepare_uploads_async(uploads, mcp_client=active_mcp)
                converter = _default_convert

            turns = ExplorationTurnService(
                repo,
                exploration,
                runtime,
                upload_files=files,
                attachment_converter=converter,
            )
            exploration.turns = turns
            application.state.exploration_turn_service = turns

            application.state.middleware_user_sync_service = None
            application.state.auth_service = None
            application.state.avatar_store = None
            application.state.employee_avatar_service = None
            application.state.platform_query_service = None
            application.state.platform_directory_service = None
            application.state.platform_a2a_service = None
            application.state.assistant_runtime = None
            application.state.assistant_consumer = None
            application.state.assistant_scheduler_runner = None
            application.state.assistant_robot_inbound = None
            application.state.assistant_robot_stream = None

            await service.start()
            if middleware.enabled:
                # 总表快照与显式登录共用唯一维护写锁，避免旧快照删除刚创建的中央登录。
                maintenance_lock = asyncio.Lock()
                repository = employees if employees is not None else MySQLEmployeeRepository()
                stack.push_async_callback(repository.close)
                client = (
                    user_sync_client
                    if user_sync_client is not None
                    else MiddlewareClient(
                        str(middleware.base_url),
                        timeout_seconds=middleware.request_timeout_seconds,
                    )
                )
                stack.push_async_callback(client.close)
                application.state.employee_repository = repository

                directory = PlatformDirectoryService(service, client, timeout_seconds=auth.middleware_timeout_seconds)
                application.state.platform_directory_service = directory

                if a2a_settings.enabled:
                    a2a_client = PlatformA2AClient(
                        str(middleware.base_url),
                        service,
                        timeout_seconds=a2a_settings.http_timeout_seconds,
                    )
                    stack.push_async_callback(a2a_client.close)
                    a2a = PlatformA2AService(
                        PlatformA2ARepository(db_base),
                        a2a_client,
                        directory,
                        a2a_settings,
                    )
                    application.state.platform_a2a_service = a2a
                    stack.push_async_callback(a2a.close)
                    await a2a.start()

                application.state.platform_query_service = PlatformQueryService(
                    service, client, repository, timeout_seconds=auth.operation_timeout_seconds
                )
                registration = MiddlewareUserSyncService(
                    sync,
                    repository,
                    service,
                    client,
                    maintenance_lock=maintenance_lock,
                    simulated_user=auth.simulated_user(),
                )
                application.state.middleware_user_sync_service = registration

                avatars = AvatarStore(avatar_settings if avatar_settings is not None else load_avatar_settings())
                stack.push_async_callback(avatars.close)
                application.state.avatar_store = avatars
                application.state.employee_avatar_service = EmployeeAvatarService(repository, avatars)

                central = MiddlewareUserAuthService(
                    service,
                    client,
                    repository,
                    maintenance_lock=maintenance_lock,
                    registration=registration,
                    timeout_seconds=auth.middleware_timeout_seconds,
                )
                registry_access = RegistryFileAccessService(file_retrieval)
                application.state.registry_file_access = registry_access
                runtime.bind_platform_services(
                    platform_directory_service=directory,
                    registry_file_access=registry_access,
                    platform_a2a_service=application.state.platform_a2a_service,
                    enable_a2a=a2a_settings.enabled,
                )

                onboarding = None
                employee_auth = auth
                if auth.mode == 'production' or assistant_settings.enabled:
                    if auth.mode != 'production':
                        assert robot_settings is not None
                        employee_auth = auth.model_copy(
                            update={
                                'dingtalk_corp_id': robot_settings.corp_id,
                                'dingtalk_app_key': robot_settings.app_key,
                                'dingtalk_app_secret': robot_settings.app_secret,
                                'dingtalk_timeout_seconds': robot_settings.timeout_seconds,
                            }
                        )
                    employee_auth.require_dingtalk_credentials()
                    dingtalk = dingtalk_client if dingtalk_client is not None else DingTalkClient(employee_auth)
                    stack.push_async_callback(dingtalk.close)
                    onboarding = EmployeeOnboardingService(
                        dingtalk,
                        repository,
                        avatars,
                        directory,
                        own_platform_code=middleware.platform_code,
                        timeout_seconds=auth.operation_timeout_seconds,
                    )
                if auth.mode == 'development':
                    backend = DeveloperAuthentication(auth, central=central, employees=repository)
                else:
                    backend = DingTalkAuthentication(dingtalk, repository, onboarding, central)
                application.state.auth_service = AuthService(auth, backend)

                stack.push_async_callback(registration.close)
                await registration.start()

                if assistant_settings.enabled:
                    assert robot_settings is not None
                    queue_client = AssistantQueueClient(
                        str(middleware.base_url),
                        settings=load_assistant_queue_settings(),
                    )
                    stack.push_async_callback(queue_client.close)
                    robot = assistant_robot_client or DingTalkRobotClient(robot_settings)
                    stack.push_async_callback(robot.close)
                    queue = AssistantQueueService(service, queue_client, platform_code=middleware.platform_code)
                    stack.push_async_callback(queue.close)

                    assistant_repository = MySQLAssistantRepository(db_base)
                    file_delivery = AssistantFileDeliveryService(
                        assistant_repository,
                        uploaded_file_repository,
                        files,
                    )
                    finish_hooks = assistant_finish_hooks
                    if finish_hooks is None:
                        assert assistant_mllm is not None
                        rendering_agent = DingTalkMessageRenderer(
                            assistant_mllm,
                            thinking_effort=assistant_settings.rendering_thinking_effort,
                        )
                        stack.push_async_callback(rendering_agent.close)
                        finish_hooks = (AssistantMessageRenderingHook(rendering_agent),)

                    delivery = AssistantDeliveryService(
                        assistant_repository,
                        queue,
                        robot,
                        file_delivery=file_delivery,
                        employees=repository,
                        publication_retry_window_seconds=assistant_settings.publication_retry_window_seconds,
                    )
                    calendar_client = DingTalkCalendarClient(employee_auth)
                    stack.push_async_callback(calendar_client.close)
                    calendar = CalendarService(repository, calendar_client)

                    scheduler_settings = SchedulerSettings(
                        heartbeat_interval_seconds=assistant_settings.heartbeat_interval_seconds,
                        poll_seconds=assistant_settings.heartbeat_poll_seconds,
                    )
                    assistant_runtime = AssistantRuntimeService(
                        repo.ensure_connected,
                        scheduler_settings=scheduler_settings,
                        finish_hooks=finish_hooks,
                        upload_files=files,
                        mcp_client=active_mcp,
                        assistant_repository=assistant_repository,
                        delivery_service=delivery,
                        file_delivery_service=file_delivery,
                        employees=repository,
                        calendar_service=calendar,
                        file_retrieval_service=file_retrieval,
                        registry_file_access=registry_access,
                        registry_file_client=registry_files,
                        platform_directory_service=directory,
                        platform_a2a_service=application.state.platform_a2a_service,
                        enable_a2a=a2a_settings.enabled,
                        file_visibility_service=file_visibility,
                        file_note_repository=AssistantFileNoteRepository(db_base),
                        portrait_repository=assistant_repository.portraits,
                    )
                    application.state.assistant_runtime = assistant_runtime
                    stack.push_async_callback(assistant_runtime.close)

                    consumer = AssistantConsumer(
                        assistant_settings,
                        assistant_repository,
                        queue,
                        delivery,
                        repository,
                        files,
                        assistant_runtime,
                        file_registration=file_registration,
                    )
                    application.state.assistant_consumer = consumer
                    stack.push_async_callback(consumer.close)
                    await consumer.start()

                    scheduler_runner = AssistantSchedulerRunner(
                        assistant_repository,
                        repository,
                        assistant_runtime,
                        poll_seconds=min(
                            assistant_settings.heartbeat_poll_seconds,
                            assistant_settings.reminder_poll_seconds,
                        ),
                        enabled=True,
                    )
                    application.state.assistant_scheduler_runner = scheduler_runner
                    stack.push_async_callback(scheduler_runner.close)
                    await scheduler_runner.start()

                    inbound = RobotInboundService(
                        assistant_settings,
                        RobotInboxRepository(db_base),
                        repository,
                        queue,
                        robot,
                        files,
                        onboarding=onboarding,
                        registration=registration,
                        reply_files=RobotReplyFileResolver(
                            assistant_repository,
                            assistant_repository,
                            files,
                            platform_code=middleware.platform_code,
                        ),
                        mcp_client=active_mcp,
                    )
                    application.state.assistant_robot_inbound = inbound
                    stack.push_async_callback(inbound.close)
                    await inbound.start()

                    stream = assistant_stream_factory(robot_settings, inbound.receive, client=robot)
                    application.state.assistant_robot_stream = stream
                    stack.push_async_callback(stream.close)
                    await stream.start()

            stack.push_async_callback(turns.close)
            await turns.start()
            yield

    application = FastAPI(title='企业知识沉淀客户后端', lifespan=lifespan)
    application.include_router(auth_router, prefix=API_PREFIX)
    application.include_router(resource_router, prefix=API_PREFIX)
    application.include_router(employee_avatar_router, prefix=API_PREFIX)
    application.include_router(platform_router, prefix=API_PREFIX)
    application.include_router(exploration_router, prefix=API_PREFIX)
    application.include_router(exploration_turn_router, prefix=API_PREFIX)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # 不返回 loc/msg/input，避免非法字段名、code 或 token 被默认 422 回显。
        return JSONResponse(status_code=422, content={'detail': '请求参数不合法'}, headers={'Cache-Control': 'no-store'})

    @application.exception_handler(ResponseValidationError)
    async def response_error(request, error):
        return JSONResponse(status_code=503, content={'detail': '用户认证响应暂不可用'}, headers={'Cache-Control': 'no-store'})

    async def auth_error(request, error):
        status = 401 if isinstance(error, InvalidCredentials) else 503
        headers = {'Cache-Control': 'no-store'}
        if status == 401:
            headers['WWW-Authenticate'] = 'Bearer'
        return JSONResponse(status_code=status, content={'detail': str(error)}, headers=headers)

    for error_type in (AuthUnavailable, InvalidCredentials):
        application.add_exception_handler(error_type, auth_error)
    return application


app = create_app()


if __name__ == '__main__':
    from pathlib import Path
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        'app.main:app',
        host='127.0.0.1',
        port=15000,
        reload=False,
        reload_dirs=[str(Path(__file__).resolve().parent)],
    )
