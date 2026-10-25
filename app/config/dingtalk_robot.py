"""单聊机器人配置；复用钉钉应用凭据，不加载登录或模拟身份配置。"""
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.config.middleware import PROJECT_ROOT


class DingTalkRobotSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)

    corp_id: str = ''
    app_key: SecretStr = Field(default=SecretStr(''), exclude=True)
    app_secret: SecretStr = Field(default=SecretStr(''), exclude=True)
    robot_code: str = ''
    timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    callback_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    reconnect_max_seconds: float = Field(default=60, ge=1, allow_inf_nan=False)
    max_pending_callbacks: int = Field(default=16, gt=0)
    max_frame_bytes: int = Field(default=1048576, gt=0)
    max_image_pixels: int = Field(default=40000000, gt=0)
    max_image_frames: int = Field(default=100, gt=0)
    max_download_bytes: int = Field(default=52428800, gt=0)
    download_allowed_hosts: tuple[str, ...] = ('dingtalk.com', 'alicdn.com', 'aliyuncs.com')

    @field_validator('download_allowed_hosts')
    @classmethod
    def validate_hosts(cls, hosts):
        import re
        if not hosts or any(not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', host) for host in hosts):
            raise ValueError('下载域名配置不合法')
        return hosts

    def require_credentials(self):
        values = (self.corp_id, self.robot_code, self.app_key.get_secret_value(), self.app_secret.get_secret_value())
        if any(not value or value != value.strip() or any(c.isspace() for c in value) for value in values):
            raise ValueError('机器人需要配置 CorpId、AppKey、AppSecret 和 RobotCode')


def load_dingtalk_robot_settings(env_file: Path | None = None) -> DingTalkRobotSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return DingTalkRobotSettings(
            corp_id=values.get('DINGTALK_CORP_ID') or '',
            app_key=values.get('DINGTALK_APP_KEY') or '',
            app_secret=values.get('DINGTALK_APP_SECRET') or '',
            robot_code=values.get('DINGTALK_ROBOT_CODE') or '',
            timeout_seconds=values.get('DINGTALK_TIMEOUT_SECONDS', '10'),
            callback_timeout_seconds=values.get('DINGTALK_ROBOT_CALLBACK_TIMEOUT_SECONDS', '10'),
            reconnect_max_seconds=values.get('DINGTALK_ROBOT_RECONNECT_MAX_SECONDS', '60'),
            max_pending_callbacks=values.get('DINGTALK_ROBOT_MAX_PENDING_CALLBACKS', '16'),
            max_frame_bytes=values.get('DINGTALK_ROBOT_MAX_FRAME_BYTES', '1048576'),
            max_image_pixels=values.get('DINGTALK_ROBOT_MAX_IMAGE_PIXELS', '40000000'),
            max_image_frames=values.get('DINGTALK_ROBOT_MAX_IMAGE_FRAMES', '100'),
            max_download_bytes=values.get('DINGTALK_ROBOT_MAX_DOWNLOAD_BYTES', '52428800'),
            download_allowed_hosts=tuple(h.strip().lower() for h in values.get(
                'DINGTALK_ROBOT_DOWNLOAD_ALLOWED_HOSTS', 'dingtalk.com,alicdn.com,aliyuncs.com').split(',')),
        )
    except (ValueError, TypeError):
        raise ValueError('钉钉机器人配置不合法，请检查凭据与运行参数') from None
