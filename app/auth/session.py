"""生成中央用户 token；不维护本地 userid 映射、过期时间或会话缓存。"""

from secrets import token_urlsafe


def new_access_token() -> str:
    """每次显式登录生成独立高熵凭据，是否生效完全以中央绑定结果为准。"""
    return token_urlsafe(32)
