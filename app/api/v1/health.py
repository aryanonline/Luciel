from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()

@router.get("/version")
def version() -> dict:
    return {
        "app": settings.app_name,
        "environment": settings.environment,
        "version": "0.1.0",
        "default_tenant_id": settings.default_tenant_id,
        "default_domain_id": settings.default_domain_id,
    }
