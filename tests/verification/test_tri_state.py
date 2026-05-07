"""Tri-state verify outcome contract tests (Step 29.y, Cluster 8).

These are pure unit tests against ``app.verification.runner``. They do
NOT require a live backend, Postgres, Redis, SQS, or any of the other
infrastructure the pillar suite actually exercises in production. The
goal is to lock the FULL/DEGRADED/FAIL contract that the rest of the
verify pipeline (CLI, JSON artifact, ECS task exit code, deployment
gate) depends on, so a future refactor of a single pillar cannot
silently re-introduce the "everything is GREEN even when half the
infrastructure is mocked out" honesty bug Step 29.y was created to
fix.

Why this file exists separately from ``test_pillars.py``:

  * ``test_pillars.py`` skips wholesale when ``BASE_URL`` is unreachable,
    which is the expected state in CI and in any developer environment
    that doesn't have a uvicorn process running. The contract this file
    enforces -- "DEGRADED must surface in JSON, must be a non-zero exit
    by default, must be allowed via --allow-degraded" -- has nothing to
    do with whether a backend is reachable. It must run in CI on every
    push so the tri-state guarantees can never silently regress.
  * Mixing live-backend tests and pure-runner unit tests in the same
    module would force the unit tests to inherit the live-backend skip,
    defeating the point.

Coverage matrix (each row = one assertion in this file):

  | Pillar return shape           | Expected outcome | Test name                                   |
  |-------------------------------|------------------|---------------------------------------------|
  | ``"some detail"`` (bare str)  | FULL             | ``test_bare_string_return_normalizes_to_full`` |
  | ``""`` (empty str)            | FULL, "ok"       | ``test_empty_string_return_normalizes_to_full_ok`` |
  | ``None``                      | FULL, "ok"       | ``test_none_return_normalizes_to_full_ok``  |
  | ``PillarOutcome(FULL, ...)``  | FULL             | ``test_pillar_outcome_full_passes_through`` |
  | ``PillarOutcome(DEGRADED, .)``| DEGRADED         | ``test_pillar_outcome_degraded_passes_through`` |
  | ``PillarOutcome(FAIL, ...)``  | DEGRADED (coerced) | ``test_pillar_outcome_fail_coerced_to_degraded`` |
  | ``raise Exception(...)``      | FAIL             | ``test_raised_exception_becomes_fail``      |

Plus three integration-shaped (still in-process) checks:

  * ``test_aggregate_fails_on_degraded_without_flag`` -- the prod-default
    gate.
  * ``test_aggregate_passes_on_degraded_with_flag``  -- the dev-loop gate.
  * ``test_json_report_surfaces_degraded_outcome``   -- the artifact
    contract that downstream dashboards rely on.

If any of these fail, the verify suite is no longer honest about its own
state and the rollout pipeline cannot be trusted.
"""

from __future__ import annotations

import json

from app.verification.runner import (
    Outcome,
    Pillar,
    PillarOutcome,
    SuiteRunner,
)


# ---------------------------------------------------------------------------
# Test doubles. Each subclass returns one specific shape so we can isolate
# the normalization branch under test without dragging in a live state.
# ---------------------------------------------------------------------------


class _BareStringPillar(Pillar):
    number = 901
    name = "test/bare-string"

    def run(self, state):  # noqa: ARG002 - state is unused on purpose
        return "ran every check"


class _EmptyStringPillar(Pillar):
    number = 902
    name = "test/empty-string"

    def run(self, state):  # noqa: ARG002
        return ""


class _NonePillar(Pillar):
    number = 903
    name = "test/none"

    def run(self, state):  # noqa: ARG002
        return None


class _OutcomeFullPillar(Pillar):
    number = 904
    name = "test/outcome-full"

    def run(self, state):  # noqa: ARG002
        return PillarOutcome(Outcome.FULL, "all subsystems live")


class _OutcomeDegradedPillar(Pillar):
    number = 905
    name = "test/outcome-degraded"

    def run(self, state):  # noqa: ARG002
        return PillarOutcome(Outcome.DEGRADED, "redis unavailable; skipped F4")


class _OutcomeFailMisusePillar(Pillar):
    """A pillar that misuses the contract by returning FAIL via PillarOutcome.

    The runner must coerce this to DEGRADED rather than crash, because
    losing a pillar's result entirely is worse than mis-categorizing it
    as DEGRADED. Failure is supposed to be expressed by raising.
    """

    number = 906
    name = "test/outcome-fail-misuse"

    def run(self, state):  # noqa: ARG002
        return PillarOutcome(Outcome.FAIL, "should not be allowed")


class _RaisingPillar(Pillar):
    number = 907
    name = "test/raises"

    def run(self, state):  # noqa: ARG002
        raise RuntimeError("simulated production failure")


# ---------------------------------------------------------------------------
# _normalize() unit tests.
# ---------------------------------------------------------------------------


def test_bare_string_return_normalizes_to_full():
    outcome, detail = SuiteRunner._normalize("ran every check")
    assert outcome == Outcome.FULL
    assert detail == "ran every check"


def test_empty_string_return_normalizes_to_full_ok():
    # An empty detail string is legal but uninformative; the runner
    # substitutes "ok" so the human banner never has a blank reason cell.
    outcome, detail = SuiteRunner._normalize("")
    assert outcome == Outcome.FULL
    assert detail == "ok"


def test_none_return_normalizes_to_full_ok():
    outcome, detail = SuiteRunner._normalize(None)
    assert outcome == Outcome.FULL
    assert detail == "ok"


def test_pillar_outcome_full_passes_through():
    outcome, detail = SuiteRunner._normalize(
        PillarOutcome(Outcome.FULL, "all subsystems live")
    )
    assert outcome == Outcome.FULL
    assert detail == "all subsystems live"


def test_pillar_outcome_degraded_passes_through():
    outcome, detail = SuiteRunner._normalize(
        PillarOutcome(Outcome.DEGRADED, "redis unavailable; skipped F4")
    )
    assert outcome == Outcome.DEGRADED
    assert detail == "redis unavailable; skipped F4"


def test_pillar_outcome_fail_coerced_to_degraded():
    # Contract: FAIL is not a legal pillar return value -- failures must
    # be raised. If a pillar returns FAIL anyway, the runner downgrades
    # to DEGRADED rather than dropping the result, but we still want the
    # detail string preserved so an operator can see what went wrong.
    outcome, detail = SuiteRunner._normalize(
        PillarOutcome(Outcome.FAIL, "should not be allowed")
    )
    assert outcome == Outcome.DEGRADED
    assert detail == "should not be allowed"


# ---------------------------------------------------------------------------
# Full-runner integration tests (still in-process, no network).
# ---------------------------------------------------------------------------


def _build_runner(*pillars: Pillar) -> SuiteRunner:
    runner = SuiteRunner()
    for p in pillars:
        runner.register(p)
    return runner


def test_raised_exception_becomes_fail():
    runner = _build_runner(_RaisingPillar())
    report = runner.run(state=None)
    assert len(report.results) == 1
    result = report.results[0]
    assert result.outcome == Outcome.FAIL
    assert "simulated production failure" in result.detail
    assert result.traceback_text is not None
    assert "RuntimeError" in result.traceback_text


def test_aggregate_fails_on_degraded_without_flag():
    """Prod-default gate: any DEGRADED must flip exit code to 1.

    This is the assertion the rollout pipeline depends on. If a pillar
    silently drops to DEGRADED in production, the verify task must not
    return 0, otherwise the deployment is gated by a lie.
    """
    runner = _build_runner(_BareStringPillar(), _OutcomeDegradedPillar())
    report = runner.run(state=None)
    assert report.full_count == 1
    assert report.degraded_count == 1
    assert report.fail_count == 0
    assert report.exit_code(allow_degraded=False) == 1
    # Default arg must also be False -- no caller should rely on a
    # silently-permissive default.
    assert report.exit_code() == 1


def test_aggregate_passes_on_degraded_with_flag():
    """Dev-loop escape hatch: --allow-degraded permits DEGRADED but not FAIL."""
    runner = _build_runner(_BareStringPillar(), _OutcomeDegradedPillar())
    report = runner.run(state=None)
    assert report.exit_code(allow_degraded=True) == 0


def test_aggregate_fails_on_fail_even_with_flag():
    """--allow-degraded must NOT mask a FAIL. FAIL always exits 1."""
    runner = _build_runner(
        _BareStringPillar(), _OutcomeDegradedPillar(), _RaisingPillar()
    )
    report = runner.run(state=None)
    assert report.fail_count == 1
    assert report.exit_code(allow_degraded=False) == 1
    assert report.exit_code(allow_degraded=True) == 1


def test_aggregate_all_full_passes_strict():
    """All FULL must exit 0 in both modes."""
    runner = _build_runner(_BareStringPillar(), _NonePillar(), _OutcomeFullPillar())
    report = runner.run(state=None)
    assert report.full_count == 3
    assert report.degraded_count == 0
    assert report.all_full is True
    assert report.exit_code(allow_degraded=False) == 0
    assert report.exit_code(allow_degraded=True) == 0


def test_empty_report_fails():
    """Zero pillars must never report success -- protects against an
    accidental registry truncation that leaves the matrix empty."""
    runner = _build_runner()
    report = runner.run(state=None)
    assert report.total_count == 0
    assert report.exit_code(allow_degraded=False) == 1
    assert report.exit_code(allow_degraded=True) == 1


def test_json_report_surfaces_degraded_outcome():
    """JSON artifact must encode tri-state outcomes as plain strings.

    Downstream dashboards and the rollout pipeline parse the artifact;
    if DEGRADED is silently emitted as ``"PASS"`` or buried inside a
    legacy ``passed`` field, the honesty fix from Cluster 8 is undone.
    """
    runner = _build_runner(
        _BareStringPillar(),
        _OutcomeDegradedPillar(),
        _RaisingPillar(),
    )
    report = runner.run(state=None)
    payload = report.to_json()

    # The summary block carries explicit tri-state counts.
    assert payload["full"] == 1
    assert payload["degraded"] == 1
    assert payload["fail"] == 1
    assert payload["total"] == 3
    assert payload["all_full"] is False

    # Each per-pillar entry exposes outcome as a bare string for
    # parsing-stability across language/runtime versions.
    by_name = {r["name"]: r for r in payload["results"]}
    assert by_name["test/bare-string"]["outcome"] == "FULL"
    assert by_name["test/outcome-degraded"]["outcome"] == "DEGRADED"
    assert by_name["test/raises"]["outcome"] == "FAIL"

    # The DEGRADED reason MUST round-trip into JSON. An operator
    # debugging from the artifact alone needs the reason without
    # having to scrape the human banner.
    assert (
        by_name["test/outcome-degraded"]["detail"]
        == "redis unavailable; skipped F4"
    )

    # The whole payload must JSON-serialize cleanly. enum.Enum mishaps
    # would surface here as a TypeError.
    json.dumps(payload)


def test_legacy_passed_field_includes_degraded():
    """``passed`` is the pre-29.y aggregate key. Downstream dashboards
    may still parse it; preserve its semantics (FULL + DEGRADED) so we
    do not break consumers we have not yet migrated. This is the only
    place where DEGRADED counts toward "passed" -- the exit code path
    keeps its strict default."""
    runner = _build_runner(
        _BareStringPillar(),
        _OutcomeDegradedPillar(),
    )
    report = runner.run(state=None)
    payload = report.to_json()
    assert payload["passed"] == 2  # 1 FULL + 1 DEGRADED, legacy semantics


def test_misused_fail_outcome_is_coerced_via_runner():
    """End-to-end: a pillar that returns ``PillarOutcome(FAIL, ...)``
    surfaces as DEGRADED in the report (not FAIL, not lost)."""
    runner = _build_runner(_OutcomeFailMisusePillar())
    report = runner.run(state=None)
    assert len(report.results) == 1
    result = report.results[0]
    assert result.outcome == Outcome.DEGRADED
    assert result.detail == "should not be allowed"
