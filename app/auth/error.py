"""用户基础设施错误，不携带钉钉响应或凭据。"""


class InvalidCredentials(ValueError):
    """钉钉 code 或开发登录凭据无效。"""


class AuthUnavailable(RuntimeError):
    """身份、员工资料或中央会话依赖暂不可用。"""

