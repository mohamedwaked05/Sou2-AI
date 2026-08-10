"""Health-check endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.session import get_db_session

router = APIRouter(tags=["health"])


@router.get("/health", summary="Check API health")
async def health_check() -> dict[str, str]:
    """Return basic service information without checking future dependencies."""
    settings = get_settings()
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }


@router.get(
    "/health/database",
    summary="Check database health",
    response_model=None,
    responses={503: {"description": "PostgreSQL is unavailable"}},
)
def database_health_check(
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """Run a minimal query and never expose connection or SQL details."""
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "healthy"})
