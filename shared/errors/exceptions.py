class AppError(Exception):
    """Base class for errors that should be rendered as the standard error envelope.

    ``{"error": {"code", "message", "request_id"}}``
    """

    code = "INTERNAL_ERROR"
    status_code = 500
    # extra response headers (e.g. colearner's X-Nool-Cost on a failed grade)
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


class ConflictError(AppError):
    """The request is well-formed, but the resource's current state doesn't
    permit it - e.g. scheduling a Test that isn't in a schedulable state,
    or confirming Homework completion that isn't in REVIEW.
    """

    code = "CONFLICT"
    status_code = 409
