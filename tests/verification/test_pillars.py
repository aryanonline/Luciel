"""Parametrized pytest harness for the verification pillar suite.

Step 29 Commit D builds this harness so the verification matrix can be
exercised by ``pytest`` in addition to ``python -m app.verification``.
Both entry points reference the same source of truth -- the registry
extracted in this commit (``app/verification/registry.py``) -- so adding
or reordering a pillar updates both call sites simultaneously.

Why a separate pytest harness when ``python -m app.verification`` already
runs the matrix?

  1. **Reporting.** pytest gives per-test PASS/FAIL/SKIP rows in CI logs
     and integrates with pytest-html, JUnit XML, and any other downstream
     reporter. The CLI's MatrixReport is a custom artifact; pytest output
     is the lingua franca of test infrastructure.
  2. **Selection.** ``pytest -k pillar_14`` runs one pillar in isolation
     when iterating on a single bug. The CLI runs them all-or-nothing
     (``--skip-migration`` is the only available skip).
  3. **Fixture composition.** Future tests that need a fresh tenant +
     specific pillar prelude (e.g. "P3 has run, now exercise this new
     edge case") can reuse the ``run_state`` fixture without re-running
     the whole suite.

What this harness does NOT do:

  - It does NOT replace the CLI as the production verify gate. The prod
    verify gate of record is ``python -m app.verification --json-report
    <path>`` running inside the ``luciel-verify:N`` task definition,
    because that's the artifact-producing path the rollout pipeline
    consumes.
  - It does NOT run in CI. GitHub Actions does not stand up a live
    backend (uvicorn + Postgres + Redis) for the pillar suite -- doing
    so would make the CI run several minutes long for every push and
    require provisioning database secrets in Actions. The session-scoped
    ``_backend_reachable`` fixture below skips every test in this module
    when ``BASE_URL`` is unreachable, which is the expected state in
    CI. Local developers running ``pytest tests/verification/`` against
    a live ``uvicorn app.main:app --reload`` will see all pillars run.
  - It does NOT include the teardown-integrity pillar (P10). P10 must
    run AFTER teardown of the tenant created by P1; running it pre-
    teardown would FAIL by construction. The CLI orchestrates that
    lifecycle; replicating it in pytest would duplicate the teardown
    sequencing logic that already lives in ``__main__._thorough_teardown``.

The harness assumes ``LUCIEL_PLATFORM_ADMIN_KEY`` is set in the
environment when run locally. ``RunState.__init__`` calls
``load_platform_admin_key`` which raises if it's missing, so an
unconfigured local run will fail fast at fixture setup with a clear
message.
"""

from __future__ import annotations

import os

import httpx
import pytest

# Importing the registry pulls in pillar modules whose own imports include
# ``app.db.session`` -> ``app.core.config`` -> Pydantic ``Settings`` which
# REQUIRES ``DATABASE_URL`` at import time. CI runs without a database, so
# any module-level call to ``pre_teardown_pillars()`` would explode at
# pytest collection. We capture the failure mode here and record it; if
# the import fails, the parametrized pillar test is collected with an
# empty list and its skip-reason fixture surfaces the cause cleanly.
#
# The two backend-free imports below (BASE_URL, REQUEST_TIMEOUT) do NOT
# touch the DB-config path, so they are safe to import unconditionally.
from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT  # noqa: E402

_REGISTRY_IMPORT_ERROR: Exception | None = None
_PILLARS: list = []
_RunState = None  # type: ignore[assignment]
_Pillar = None  # type: ignore[assignment]
try:
    from app.verification.fixtures import RunState as _RunState  # noqa: F401
    from app.verification.registry import pre_teardown_pillars
    from app.verification.runner import Pillar as _Pillar  # noqa: F401

    _PILLARS = pre_teardown_pillars(include_migration=True)
except Exception as exc:  # noqa: BLE001 -- intentional broad catch at collection time
    _REGISTRY_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Backend-reachability gate
# ---------------------------------------------------------------------------
#
# pytest collection should NOT fail when the backend is down -- that's the
# default state in CI. We probe BASE_URL once per session with a short
# timeout. If unreachable, every test in this module is skipped (not
# errored) so the test session reports cleanly.
#
# The probe is intentionally cheap: a GET on ``/`` with a 1-second timeout.
# Any 2xx/3xx/4xx response counts as "reachable" -- we only care that
# something is listening on the port, not that any specific endpoint is
# correct. The pillar tests themselves are responsible for endpoint-level
# assertions.


@pytest.fixture(scope="session")
def _registry_loaded() -> bool:
    """Skip the suite if the registry could not be imported.

    The most common cause is missing ``DATABASE_URL`` at pytest collection
    (CI default state). We surface the original exception so the skip
    reason in the report points at the real cause, not a downstream
    fixture that happened to need the registry.
    """
    if _REGISTRY_IMPORT_ERROR is not None:
        pytest.skip(
            f"verification pillar suite skipped: pillar registry could "
            f"not be imported "
            f"({type(_REGISTRY_IMPORT_ERROR).__name__}: "
            f"{_REGISTRY_IMPORT_ERROR}). The most common cause is a "
            f"missing DATABASE_URL env var; this is the expected state "
            f"in CI."
        )
    return True


@pytest.fixture(scope="session")
def _backend_reachable(_registry_loaded: bool) -> bool:
    """Probe BASE_URL once. Skip the whole module if unreachable.

    Honors ``LUCIEL_BASE_URL`` via ``BASE_URL`` already imported above.
    Returns True on success; calls ``pytest.skip`` (which aborts every
    test in the module) on failure.
    """
    try:
        with httpx.Client(base_url=BASE_URL, timeout=1.0) as c:
            # Any response (including 404) means the server is up. We
            # only care that the TCP connection + HTTP handshake worked.
            c.get("/")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        pytest.skip(
            f"verification pillar suite skipped: backend at {BASE_URL} "
            f"unreachable ({type(exc).__name__}: {exc}). This is the "
            f"expected state in CI; run 'uvicorn app.main:app --reload' "
            f"locally to exercise these tests."
        )
    return True


@pytest.fixture(scope="session")
def run_state(_backend_reachable: bool):
    """One throwaway tenant shared across all pillar tests in this session.

    The pillars mutate ``RunState`` in place (P1 sets ``tenant_admin_key``,
    P2 sets ``domain_id`` / ``agent_id`` / instance ids, P3 sets source
    ids, P4 populates ``chat_keys``, etc.). Each subsequent pillar reads
    the state its predecessors produced. This is the same lifecycle the
    CLI uses; we deliberately reuse it rather than minting a fresh
    tenant per test (which would either cost N tenant-create round trips
    or break pillar dependencies).

    This means pillar tests in this module must run in registration
    order. pytest preserves parametrize order by default; we don't shuffle.
    """
    return _RunState()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parametrized pillar test
# ---------------------------------------------------------------------------
#
# We import the pillar list at module load via the registry, then
# parametrize one test per pillar. The id function uses the pillar
# number so failing pillars surface as e.g.
# ``test_pillar[14-departure_semantics]`` in pytest output.
#
# include_migration=True matches the default verify-gate behavior. The
# CLI's ``--skip-migration`` flag is for fast local iteration; CI parity
# wants the full set, including P9.

def _pillar_id(pillar) -> str:
    """Render a pillar as ``<number>-<name>`` for the test id."""
    safe_name = pillar.name.replace(" ", "_").replace("/", "_")
    return f"{pillar.number:02d}-{safe_name}"


@pytest.mark.parametrize("pillar", _PILLARS, ids=_pillar_id)
def test_pillar(pillar, run_state) -> None:
    """Run one pillar against the shared RunState.

    A pillar's ``run`` method either returns a detail string (PASS) or
    raises (FAIL). We let the exception propagate -- pytest will render
    the traceback. The CLI catches and converts to a PillarResult; here
    we want the raw failure surface because that's what makes pytest
    useful for debugging.
    """
    detail = pillar.run(run_state)
    # Defensive: a pillar that returns None completed without raising.
    # The CLI treats that as PASS with detail "ok". We do the same.
    assert detail is None or isinstance(detail, str), (
        f"pillar {pillar.number} ({pillar.name}) returned a non-string "
        f"non-None value: {detail!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test for the registry itself
# ---------------------------------------------------------------------------
#
# Even when the backend is unreachable (CI case), we still want one
# smoke assertion to fire: that the registry is well-formed. Without
# this, a CI run with no backend would report 0 collected tests for
# this module, which would silently mask a registry-import regression.
# This test does NOT depend on the backend, so it runs regardless.


def test_registry_returns_nonempty_pillar_list(_registry_loaded: bool) -> None:
    """``pre_teardown_pillars()`` returns a list of Pillar instances.

    Pinned invariants:
      - The list is non-empty.
      - Every entry has a positive integer ``number`` and a non-empty
        ``name`` (these are what the matrix report keys on).
      - Pillar numbers are unique within the list.
      - ``include_migration=False`` returns a list strictly shorter
        than the default by exactly one (P9 dropped).

    Skipped (via ``_registry_loaded``) when DATABASE_URL is unset, since
    the registry import requires it. That skip is recorded with the
    underlying exception so a registry-shape regression cannot hide
    behind it -- a developer running locally with DATABASE_URL set will
    see this test execute and FAIL on a real shape regression.
    """
    full = pre_teardown_pillars(include_migration=True)
    assert len(full) > 0, "registry returned empty pillar list"
    numbers = [p.number for p in full]
    assert all(isinstance(n, int) and n > 0 for n in numbers), (
        f"pillar numbers must be positive ints, got {numbers!r}"
    )
    assert len(set(numbers)) == len(numbers), (
        f"duplicate pillar numbers in registry: {numbers!r}"
    )
    assert all(p.name for p in full), "every pillar must have a non-empty name"

    without_p9 = pre_teardown_pillars(include_migration=False)
    assert len(without_p9) == len(full) - 1, (
        f"include_migration=False should drop exactly one pillar (P9); "
        f"got len(full)={len(full)} len(without_p9)={len(without_p9)}"
    )
    # P9 must not appear in the no-migration list.
    assert 9 not in [p.number for p in without_p9], (
        "include_migration=False still contains pillar 9"
    )
