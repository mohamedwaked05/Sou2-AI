"""Central registration point for version 1 API routes."""

from fastapi import APIRouter

from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.businesses import router as businesses_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.owner_chat import router as owner_chat_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(businesses_router)
api_router.include_router(owner_chat_router)
