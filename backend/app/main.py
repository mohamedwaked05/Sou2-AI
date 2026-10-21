"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestSecurityMiddleware
from app.core.runtime import (
    WORKER_CONTRACT_VERSION,
    source_version,
    worker_configuration_identity,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the Sou2AI FastAPI application."""
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.environment)
    running_source = source_version()
    try:
        running_worker_configuration = worker_configuration_identity(app_settings)
    except RuntimeError:
        running_worker_configuration = "unconfigured"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Starting %s version %s in %s environment",
            app_settings.app_name,
            app_settings.app_version,
            app_settings.environment,
        )
        logger.info(
            "api_runtime source_version=%s contract_version=%s provider=%s",
            running_source,
            WORKER_CONTRACT_VERSION,
            app_settings.owner_chat_provider,
        )
        yield
        logger.info("Stopping %s", app_settings.app_name)

    application = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        # Raw debug tracebacks are never safe on an HTTP response. The setting
        # remains available for non-response diagnostics only.
        debug=False,
        lifespan=lifespan,
        docs_url="/docs" if app_settings.docs_enabled else None,
        redoc_url="/redoc" if app_settings.docs_enabled else None,
        openapi_url="/openapi.json" if app_settings.docs_enabled else None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    application.include_router(api_router, prefix=app_settings.api_v1_prefix)
    register_exception_handlers(application)
    application.add_middleware(RequestSecurityMiddleware, settings=app_settings)

    @application.get("/", tags=["root"])
    async def root() -> dict[str, str | int]:
        """Provide basic API metadata for a simple local connectivity check."""
        metadata = {
            "service": app_settings.app_name,
            "version": app_settings.app_version,
            "source_version": running_source,
            "worker_contract_version": WORKER_CONTRACT_VERSION,
            "admission_mode": app_settings.admission_mode,
            "worker_configuration_sha256": running_worker_configuration,
            "environment": app_settings.environment,
            "status": "running",
            "api": app_settings.api_v1_prefix,
        }
        if app_settings.docs_enabled:
            metadata["docs"] = "/docs"
        return metadata

    return application


app = create_app()
