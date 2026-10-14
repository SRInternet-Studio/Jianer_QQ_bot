from ..exceptions import HTTPError, TokenError, UserNotFoundError


class LXNSParamsError(HTTPError):
    """参数错误"""


class LXNSPermissionDeniedError(HTTPError):
    """权限不足"""


class LXNSNotFoundError(HTTPError):
    """未找到资源"""


class LXNSTooManyRequestsError(HTTPError):
    """过多的请求"""


class LXNSOAuthError(HTTPError):
    """OAuth2错误"""

    def __init__(
        self,
        error: str = "oauth_error",
        error_description: str | None = None,
    ) -> None:
        self.error = str(error or "oauth_error")[:128]
        self.error_description = (
            str(error_description)[:500]
            if error_description is not None
            else None
        )
        detail = self.error
        if self.error_description:
            detail += f": {self.error_description}"
        super().__init__(detail)


class LXNSTokenError(TokenError):
    """用户Token错误"""


class LXNSUserNotFoundError(UserNotFoundError):
    """未找到用户"""
