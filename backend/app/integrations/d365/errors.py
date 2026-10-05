"""Stable integration error codes; messages never contain credential material."""

from app.core.errors import AppError


class D365AuthenticationError(AppError):
    def __init__(
        self,
        message="Dynamics 365 authentication failed. Check the tenant, app registration, secret, and D365 Entra mapping.",
    ):
        super().__init__("D365_AUTHENTICATION_ERROR", message, status_code=503)


class D365ConnectionError(AppError):
    def __init__(
        self, message="Dynamics 365 is currently unavailable. Reconnect to continue.", retryable=True
    ):
        super().__init__("D365_CONNECTION_ERROR", message, status_code=503, retryable=retryable)


class D365EntityUnavailableError(AppError):
    def __init__(self, message, details=None):
        super().__init__("D365_CAPABILITY_UNAVAILABLE", message, status_code=422, details=details)


class UnsafeMutationError(AppError):
    def __init__(self, message):
        super().__init__("UNSAFE_MUTATION", message, status_code=409)


def map_http_error(status):
    if status == 401:
        return D365AuthenticationError(
            "Dynamics 365 rejected refreshed authentication. Reconnect and check application access."
        )
    if status == 403:
        return AppError(
            "D365_PERMISSION_ERROR",
            "Dynamics 365 denied this operation. Check the mapped user's security roles.",
            status_code=403,
        )
    if status == 404:
        return AppError(
            "D365_ENTITY_UNAVAILABLE",
            "Dynamics 365 entity or record was not found. Check metadata diagnostics.",
            status_code=404,
        )
    if status == 429:
        return AppError(
            "D365_RATE_LIMIT_ERROR",
            "Dynamics 365 is rate limiting requests. Try again shortly.",
            status_code=503,
            retryable=True,
        )
    if status >= 500:
        return D365ConnectionError("Dynamics 365 returned a server error. Reconnect before continuing.")
    return AppError(
        "D365_VALIDATION_ERROR",
        "Dynamics 365 rejected the operation. Check the entity fields, business state, and setup configuration.",
        status_code=422,
        details={"upstream_status": status},
    )
