class AppError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int = 400, retryable: bool = False, details=None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details

    def public(self, request_id: str = "") -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
            "retryable": self.retryable,
            "details": self.details,
        }
