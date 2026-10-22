"""Agent 运行配置在模式装配时读取；导入模块不读取环境或创建资源。"""
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from dotenv import dotenv_values
from app.config.middleware import PROJECT_ROOT

if TYPE_CHECKING:
    from app.agent.core import CoreSettings


def load_agent_core_settings(env_file: Path | None = None) -> 'CoreSettings':
    # 保持低层 Core 的显式依赖；延迟导入也避免配置模块反向触发整个 Agent 包。
    from app.agent.core import CoreSettings

    values = {**dotenv_values(env_file if env_file is not None else PROJECT_ROOT / '.env',
                             interpolate=False), **os.environ}
    fields = {'AGENT_FILE_CACHE_MAX_FILES': 'file_cache_max_files',
              'AGENT_PAGE_DISPLAY_ROUNDS': 'page_display_rounds',
              'AGENT_EXPERT_SESSION_CACHE_MAX_SESSIONS': 'expert_session_cache_max_sessions',
              **{'AGENT_' + name.upper(): name for name in (
                  'web_search_cache_max_queries', 'web_search_page_size', 'web_search_max_concurrent_requests',
                  'web_page_cache_max_entries', 'web_page_chars',
                  'web_image_max_candidates', 'web_image_max_selected', 'web_image_max_bytes')}}
    options = {}
    for key, field in fields.items():
        if key not in values:
            continue
        value = values[key]
        # 配置字符串只接受十进制正整数，不把小数、布尔值或空值静默截断/回退。
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
            raise ValueError(f'{key} 必须是大于 0 的整数')
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f'{key} 必须是大于 0 的整数') from None
        if number < 1:
            raise ValueError(f'{key} 必须是大于 0 的整数')
        options[field] = number
    return CoreSettings(**options)
