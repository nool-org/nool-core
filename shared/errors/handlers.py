from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..logging.context import get_request_id
from ..logging.setup import get_logger
from .exceptions import AppError

logger = get_logger("errors")


def _error_body(code: str, message: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": get_request_id(),
        }
    }


def validation_message(errors) -> str:
    """The first error as "<field>: <reason>" (the model's own text for a
    model-level rule) - never the submitted input itself."""
    if not errors:
        return "Invalid request."
    first = errors[0]
    reason = str(first.get("msg", "invalid")).removeprefix("Value error, ")
    field = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path", "header"))
    return f"{field}: {reason}" if field else reason


def register_exception_handlers(app: FastAPI) -> None:
    """Registers handlers so every error - expected or not - renders the
    standard error envelope and never leaks stack traces, DB errors, or
    internal paths to the client.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body("VALIDATION_ERROR", validation_message(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=_error_body(code, message))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content=_error_body("INTERNAL_ERROR", "An unexpected error occurred."),
        )
