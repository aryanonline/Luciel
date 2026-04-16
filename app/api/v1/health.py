from fastapi import APIRouter, Request
from app.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/version")
@limiter.limit("60/minute")
def version(request: Request) -> dict:
    return {
        "app": "Luciel Backend",
        "version": "0.1.0",
        "status": "ok",
    }