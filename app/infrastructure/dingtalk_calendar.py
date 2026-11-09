"""企业内部应用只读日程适配；复用钉钉应用认证，不提供日程写入操作。"""
import logging
import json
from urllib.parse import quote

import httpx

from app.auth.error import AuthUnavailable
from app.infrastructure.dingtalk import DingTalkClient
from app.model.calendar import CalendarEventPage, CalendarReadError

logger = logging.getLogger(__name__)


class DingTalkCalendarClient(DingTalkClient):
    async def get_unionid(self, userid: str) -> str:
        try:
            data = await self._legacy_call('/topapi/v2/user/get', {'userid': userid, 'language': 'zh_CN'})
        except AuthUnavailable:
            raise CalendarReadError('calendar_identity_unavailable',
                '未能取得该员工的钉钉日历身份，未建立日程集。请核对员工 userid、钉钉通讯录状态及应用通讯录可见范围；'
                '必要时请管理员检查通讯录读取权限，不能据此判断没有日程。') from None
        unionid = data.get('unionid')
        if data.get('userid') != userid or not isinstance(unionid, str) or not unionid.strip():
            raise CalendarReadError('calendar_identity_invalid',
                '钉钉返回的员工身份不一致或缺少 unionId，未查询日程。请管理员核对员工与钉钉身份关联后重试，不要猜测 unionId。')
        return unionid

    async def events_view(self, unionid: str, *, start_time: str, end_time: str,
                          next_token: str | None = None) -> CalendarEventPage:
        path = '/v1.0/calendar/users/' + quote(unionid, safe='') + '/calendars/primary/eventsview'
        params = {'timeMin': start_time, 'timeMax': end_time, 'maxResults': 100}
        if next_token:
            params['nextToken'] = next_token
        for attempt in range(2):
            try:
                token = await self._application_token()
            except AuthUnavailable:
                raise CalendarReadError('calendar_auth_unavailable',
                    '钉钉应用认证暂不可用，未建立日程集。请稍后重试；持续失败时请管理员检查应用凭据，不能据此判断员工空闲。') from None
            try:
                # 限制单页响应大小，防止外部异常响应占满助理进程内存。
                async with self._client.stream('GET', 'https://api.dingtalk.com' + path,
                        headers={'x-acs-dingtalk-access-token': token}, params=params) as response:
                    status = response.status_code
                    if status == 401:
                        async with self._lock:
                            if self._token is not None and self._token.get_secret_value() == token:
                                self._token = None
                        if attempt == 0:
                            continue
                        raise CalendarReadError('calendar_auth_unavailable',
                            '刷新钉钉应用凭据后仍未通过认证，未建立日程集。请管理员核对应用凭据和授权，不要重复尝试不同员工身份。')
                    if status == 403:
                        raise CalendarReadError('calendar_access_denied',
                            '钉钉拒绝读取该员工的日程，未建立日程集。请管理员检查 Calendar.Event.Read 权限、应用授权范围及日历访问权限；'
                            '不能通过更换或猜测身份绕过限制，也不能据此判断没有日程。')
                    if status == 429:
                        raise CalendarReadError('calendar_rate_limited',
                            '钉钉日程查询被限流，未建立日程集。请稍后再试，避免连续重复查询；不能据此判断员工空闲。')
                    if status in (400, 404):
                        raise CalendarReadError('calendar_request_rejected',
                            '钉钉未接受该日程查询或目标日历不可用，未建立日程集。请核对员工 userid 和带时区的时间范围；'
                            '参数正确仍失败时请管理员检查员工主日历与授权范围。')
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024:
                            raise CalendarReadError('calendar_response_too_large',
                                '钉钉返回的单页日程数据过大，未建立日程集。请缩小查询时间范围后重试，不能把部分内容当作完整结果。')
                payload = json.loads(body)
                if not isinstance(payload, dict) or 'code' in payload or payload.get('errcode', 0) != 0:
                    raise ValueError('响应不是成功业务对象')
                # 实测空日历响应只含 requestId；只兼容这一明确成功形态，不能把任意缺失列表降级为空。
                if ('events' not in payload and set(payload) <= {'requestId', 'nextToken'}
                        and isinstance(payload.get('requestId'), str) and payload['requestId'].strip()):
                    payload = {**payload, 'events': []}
                return CalendarEventPage.model_validate(payload)
            except CalendarReadError:
                raise
            except httpx.HTTPError as error:
                logger.warning('钉钉日程请求失败：error_type=%s', type(error).__name__)
                raise CalendarReadError('calendar_unavailable',
                    '钉钉日程服务暂不可用，未建立日程集。请稍后重试，不能将查询失败解释为没有日程或员工空闲。') from None
            except (ValueError, TypeError):
                raise CalendarReadError('calendar_response_invalid',
                    '钉钉日程响应缺少有效列表或时间信息，未建立日程集。请稍后重试；持续失败时请管理员排查接口响应，不要采用部分结果。') from None
