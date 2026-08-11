"""Application-specific exceptions and JSON exception handlers."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class ApplicationError(Exception):
    """Base exception for expected application-level failures."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        error_code: str = "application_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail
        self.status_code = status_code
        self.error_code = error_code
        self.details = details
        super().__init__(detail)


async def application_error_handler(
    request: Request, exc: ApplicationError
) -> JSONResponse:
    """Return a structured response for known application errors."""
    error: dict[str, Any] = {"code": exc.error_code, "message": exc.detail}
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected failures and avoid leaking details outside development."""
    logger.exception("Unhandled exception while processing %s", request.url.path)
    settings = get_settings()
    message = str(exc) if settings.debug else "An unexpected server error occurred."
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"code": "internal_server_error", "message": message}},
    )


async def safe_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return useful validation locations without echoing credential input."""
    errors = [
        {
            "location": list(error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "fields": errors,
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register the small shared exception surface for the application."""
    app.add_exception_handler(ApplicationError, application_error_handler)
    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
