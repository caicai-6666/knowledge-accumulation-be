"""单聊 Stream 连接与确认；可靠入队通过注入的异步接收器完成，不在回调中运行模型。"""
import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable

from websockets.asyncio.client import connect

from app.config.dingtalk_robot import DingTalkRobotSettings
from app.infrastructure.dingtalk_robot import DingTalkRobotClient, ROBOT_TOPIC, RobotUnavailable
from app.model.dingtalk_robot import RobotMessage, RobotMessageInvalid, parse_robot_message

logger = logging.getLogger(__name__)
# WebSocket 调试日志可包含 ticket、消息或下载码；只由本适配器输出脱敏状态。
_wire_logger = logging.Logger('dingtalk_robot.wire')
_wire_logger.addHandler(logging.NullHandler())
_wire_logger.propagate = False
_wire_logger.disabled = True


class _DirectConnect(connect):
    def process_redirect(self, exc):
        # 凭证只交给 connections/open 返回并通过校验的端点，不跟随握手重定向。
        return exc


def _decode(raw, limit):
    try:
        if not isinstance(raw, (str, bytes)) or len(raw.encode() if isinstance(raw, str) else raw) > limit:
            raise ValueError('帧超限')
        frame = json.loads(raw)
        headers = frame['headers']
        if (frame.get('specVersion') != '1.0' or not isinstance(headers, dict)
                or not isinstance(headers.get('messageId'), str) or not headers['messageId']
                or not isinstance(headers.get('topic'), str)
                or headers.get('contentType', 'application/json') != 'application/json'
                or not isinstance(frame.get('data'), str)):
            raise ValueError('无效帧')
        data = json.loads(frame['data'])
        return frame, data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RobotUnavailable('stream_frame') from None


def _ack(frame, code, data=None):
    return {'code': code, 'message': {200: 'OK', 404: 'not implemented', 500: 'internal error'}[code],
        'headers': {'messageId': frame['headers']['messageId'], 'contentType': 'application/json'},
        'data': json.dumps({'response': None} if data is None else data, ensure_ascii=False)}


class DingTalkRobotStream:
    def __init__(self, settings: DingTalkRobotSettings, receive: Callable[[RobotMessage], Awaitable[bool]],
                 *, client=None, connector=_DirectConnect):
        settings.require_credentials()
        if not callable(receive):
            raise ValueError('机器人需要显式注入可靠接收器')
        self.settings = settings
        self.client = client or DingTalkRobotClient(settings)
        self._owns_client = client is None
        self._connector = connector
        self._receive = receive
        self._inflight = {}
        self._runner = None
        self._task = None
        self._closed = False

    async def _accept(self, message):
        try:
            async with asyncio.timeout(self.settings.callback_timeout_seconds):
                accepted = await self._receive(message)
            # None 或仅创建后台 task 都不代表已经可靠接收；接收器必须显式返回 True。
            return 200 if accepted is True else 500
        except Exception as error:
            logger.warning('机器人接收失败：error_type=%s', type(error).__name__)
            return 500

    async def handle_frame(self, raw):
        """仅处理可信 Stream 帧；返回协议 ACK 和是否应断开，不是面向公网的回调路由。"""
        if self._closed:
            raise RuntimeError('机器人 Stream 已关闭')
        frame, data = _decode(raw, self.settings.max_frame_bytes)
        kind, topic = frame.get('type'), frame['headers']['topic']
        if kind == 'SYSTEM':
            return _ack(frame, 200 if topic in ('ping', 'disconnect') else 404, data), topic == 'disconnect'
        if kind != 'CALLBACK' or topic != ROBOT_TOPIC:
            return _ack(frame, 404), False
        try:
            message = parse_robot_message(data, self.settings)
        except RobotMessageInvalid:
            logger.warning('机器人消息格式校验失败')
            return _ack(frame, 500), False
        if message is None:
            # 群聊、其他企业或其他机器人明确不属于此消费者；确认忽略而不是发布任务。
            return _ack(frame, 200), False
        key = message.request_id
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._accept(message))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._inflight.pop(key, None)
                                   if self._inflight.get(key) is done else None)
        # 同一消息并发重推共用正在进行的接收；成功后的持久去重由 receive 按 request_id 保证。
        code = await asyncio.shield(task)
        return _ack(frame, code), False

    async def _serve(self, websocket):
        pending = set()

        async def respond(raw):
            reply, disconnect = await self.handle_frame(raw)
            await websocket.send(json.dumps(reply, ensure_ascii=False))
            return disconnect

        def finished(task):
            pending.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.warning('机器人回调确认发送失败')

        try:
            async for raw in websocket:
                frame, _ = _decode(raw, self.settings.max_frame_bytes)
                if frame.get('type') == 'SYSTEM':
                    # 慢入队不能挡住系统 ping；本协议心跳与助理业务心跳无关。
                    if await respond(raw):
                        return
                elif len(pending) >= self.settings.max_pending_callbacks:
                    await websocket.send(json.dumps(_ack(frame, 500)))
                else:
                    task = asyncio.create_task(respond(raw))
                    pending.add(task)
                    task.add_done_callback(finished)
        finally:
            # 连接丢失时未确认输入可以重推；接收器必须能处理外部已提交但 ACK 丢失的情况。
            tasks = set(pending) | set(self._inflight.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        if self._closed or self._runner is not None:
            raise RuntimeError('机器人 Stream 已关闭或已启动')
        self._runner = asyncio.current_task()
        failures = 0
        try:
            while True:
                try:
                    uri = await self.client.open_stream()
                    async with self._connector(uri.get_secret_value(), proxy=None, logger=_wire_logger,
                            open_timeout=self.settings.timeout_seconds, close_timeout=self.settings.timeout_seconds,
                            ping_interval=20, ping_timeout=20, max_size=self.settings.max_frame_bytes,
                            max_queue=self.settings.max_pending_callbacks) as socket:
                        await self._serve(socket)
                except Exception as error:
                    logger.warning('机器人 Stream 连接中断：error_type=%s', type(error).__name__)
                # 每次重新获取一次性 ticket；即使服务端立即断开也退避，避免连接风暴。
                delay = min(self.settings.reconnect_max_seconds, 2 ** min(failures, 10))
                await asyncio.sleep(min(self.settings.reconnect_max_seconds, delay + random.uniform(0, .5)))
                failures += 1
        finally:
            self._runner = None

    async def start(self):
        if self._closed or self._task is not None or self._runner is not None:
            raise RuntimeError('机器人 Stream 已关闭或已启动')
        self._task = asyncio.create_task(self.run())

    async def stop(self):
        # start 后立即关闭时 run 可能还没执行，仍须取消已创建的任务。
        runner = self._runner or self._task
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def close(self):
        self._closed = True
        await self.stop()
        if self._owns_client:
            await self.client.close()
