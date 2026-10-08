from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.connection import check_database

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "environment": settings.app_env}


@router.get("/health/db")
def health_db() -> JSONResponse:
    if check_database():
        return JSONResponse({"status": "ok", "database": "reachable"})
    return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
