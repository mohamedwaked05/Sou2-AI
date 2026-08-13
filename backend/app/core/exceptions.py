"""Application-specific exceptions and JSON exception handlers."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail
        self.status_code = status_code
        self.error_code = error_code
        self.details = details
        self.headers = headers or {}
        super().__init__(detail)


async def application_error_handler(
    request: Request, exc: ApplicationError
) -> JSONResponse:
    """Return a structured response for known application errors."""
    error: dict[str, Any] = {
        "code": exc.error_code,
        "message": exc.detail,
        "request_id": request.state.request_id,
    }
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=exc.headers,
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected failures and avoid leaking details outside development."""
    logger.exception(
        "Unhandled request exception request_id=%s", request.state.request_id
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_server_error",
                "message": "An unexpected server error occurred.",
                "request_id": request.state.request_id,
            }
        },
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
                "request_id": request.state.request_id,
            }
        },
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalize framework routing and protocol errors without leaking internals."""
    messages = {
        400: "The request is invalid.",
        404: "The requested resource was not found.",
        405: "The request method is not allowed.",
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": messages.get(
                    exc.status_code, "The request could not be completed."
                ),
                "request_id": request.state.request_id,
            }
        },
        headers=exc.headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register the small shared exception surface for the application."""
    app.add_exception_handler(ApplicationError, application_error_handler)
    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
