"""Public health + build-identity endpoints.

The ``/api/v1/version`` route is the canonical surface for "which
build is live?" -- exempted from api-key auth via SKIP_AUTH_PATHS at
``app/middleware/auth.py`` so operators and uptime monitors can read
it without holding a JWT.

Arc 8 Work-Unit 3 (D-version-endpoint-hardcoded-not-build-sha-2026-05-22):
the response payload now reflects the build-time git SHA threaded
through the Dockerfile's ``BUILD_GIT_SHA`` ARG/ENV chain. See
``app/core/build_info.py`` for the singleton source of truth.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.core.build_info import BUILD_INFO
from app.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/version")
@limiter.limit("60/minute")
def version(request: Request) -> dict[str, Any]:
    """Return the build identity of this process.

    Response shape (Arc 8 WU-3):
        {
          "app":     "Luciel Backend",
          "version": "0.1.0",          # from pyproject.toml
          "git_sha": "<short-sha>",    # baked at docker build time;
                                       # "unknown" if BUILD_GIT_SHA
                                       # was not passed at build time
                                       # (local dev) or the env var
                                       # is unset (non-container run).
          "status":  "ok"
        }

    The shape is a strict superset of the pre-WU-3 payload (the
    three original keys ``app``, ``version``, ``status`` are
    preserved verbatim), so existing consumers do not break on
    the rollout; new consumers read ``git_sha`` to distinguish
    one deploy from another.

    Public surface -- no auth gate. Rate-limited at 60 req/min to
    prevent a runaway monitor from drowning the endpoint.
    """
    # Return a fresh dict copy so a mutating client cannot poison
    # the module-level BUILD_INFO singleton for subsequent callers.
    return dict(BUILD_INFO)
