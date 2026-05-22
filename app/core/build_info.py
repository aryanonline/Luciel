"""Arc 8 Work-Unit 3 -- build identity singleton.

D-version-endpoint-hardcoded-not-build-sha-2026-05-22 resolution.

Single canonical surface for "which build is live?" facts. Read once
at process start (module import); exposed as the ``BUILD_INFO``
dictionary singleton. Both the ``/api/v1/version`` endpoint and any
future audit-log emission that wants to record the producing build
identity import from here.

Three identity fields:

  * **app**          -- the static app name (``"Luciel Backend"``).
                        Carried for backward compat with the previous
                        hardcoded payload so the marketing-site
                        consumer does not break on the rollout.
  * **version**      -- the semver-shaped string from
                        ``pyproject.toml`` (read at import time via
                        importlib.metadata). Today this is the
                        documented surface for human-readable
                        product versions; ``git_sha`` is the
                        machine-readable build identity.
  * **git_sha**      -- the short git SHA of the commit baked into
                        the image at ``docker build`` time. The
                        Dockerfile threads it through the
                        ``BUILD_GIT_SHA`` ARG -> ENV chain; the
                        deploy script passes
                        ``--build-arg BUILD_GIT_SHA=$(git rev-parse
                        --short HEAD)``. Defaults to ``"unknown"``
                        when the env var is unset (local dev build,
                        bare ``docker build`` without the build-arg).
  * **status**       -- always ``"ok"`` for now. Reserved for a
                        future readiness signal that distinguishes
                        "process up but DB unreachable" from "fully
                        ready"; today the existence of a 200
                        response is sufficient.

Why a module-level singleton and not a function:

  * The git SHA is fixed for the life of the process (it is the
    image's build-time identity), so caching it at module load is
    correct.
  * The version string is also fixed for the life of the process.
  * Reading from os.environ at every request would be needless work
    on a high-traffic public endpoint.

The module-level read is intentionally tolerant of missing values
(empty string env var, importlib.metadata failure) -- the endpoint
must never 500 because of a build-identity gap. Failures
gracefully degrade to ``"unknown"`` strings so the field is always
present in the response.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _read_git_sha() -> str:
    """Read BUILD_GIT_SHA from env at import time.

    Empty or unset -> "unknown". The Dockerfile's ARG/ENV default
    is already "unknown", but a process started outside the
    container (local uvicorn dev) won't have the env var at all,
    so we double-default here.
    """
    value = os.environ.get("BUILD_GIT_SHA", "").strip()
    if not value:
        return "unknown"
    return value


def _read_app_version() -> str:
    """Read the package version from importlib.metadata.

    The package is ``luciel-backend`` per pyproject.toml. Failures
    (PackageNotFoundError when running from a non-installed checkout,
    or any other lookup error) degrade to "unknown" so the endpoint
    never 500s.
    """
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("luciel-backend")
    except Exception as exc:  # pragma: no cover - defensive
        logger.info(
            "build_info.version_lookup_failed type=%s",
            type(exc).__name__,
        )
        return "unknown"


BUILD_INFO: dict[str, Any] = {
    "app": "Luciel Backend",
    "version": _read_app_version(),
    "git_sha": _read_git_sha(),
    "status": "ok",
}
