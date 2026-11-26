"""Domain-level application errors.

Every error the business layer raises is an ``AppError`` (or a subclass); the
single handler registered in ``app.main`` turns it into the fixed error
envelope ``{"code", "message", "data", "trace_id"}``. Anything that is not an
``AppError`` is left to FastAPI/Starlette and is never swallowed here.
"""


class AppError(Exception):
    code = 400
    http_status = 400

    def __init__(self, message: str | None = None, *, detail: dict | None = None) -> None:
        self.message = message or "bad request"
        self.detail = detail
        super().__init__(self.message)


class Unauthorized(AppError):
    code = http_status = 401


class Forbidden(AppError):
    code = http_status = 403


class NotFound(AppError):
    code = http_status = 404


class Conflict(AppError):
    code = http_status = 409


class RateLimited(AppError):
    code = http_status = 429


class ServiceUnavailable(AppError):
    code = http_status = 503


class PayloadTooLarge(AppError):
    code = http_status = 413


class InsufficientStorage(AppError):
    code = http_status = 507
