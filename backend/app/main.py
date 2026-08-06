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

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the Sou2AI FastAPI application."""
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Starting %s version %s in %s environment",
            app_settings.app_name,
            app_settings.app_version,
            app_settings.environment,
        )
        yield
        logger.info("Stopping %s", app_settings.app_name)

    application = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        debug=app_settings.debug,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(api_router, prefix=app_settings.api_v1_prefix)
    register_exception_handlers(application)

    @application.get("/", tags=["root"])
    async def root() -> dict[str, str]:
        """Provide basic API metadata for a simple local connectivity check."""
        return {
            "service": app_settings.app_name,
            "version": app_settings.app_version,
            "environment": app_settings.environment,
            "status": "running",
            "api": app_settings.api_v1_prefix,
            "docs": "/docs",
        }

    return application


app = create_app()
