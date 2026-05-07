"""Step 26 verification runner.

Contract:
  - Pillars declare themselves via the `Pillar` ABC.
  - SuiteRunner executes pillars in declared order (no parallelism,
    no dependency resolution -- ordering is the explicit contract).
  - Every pillar runs, even if a prior one failed (run-all-then-report).
  - PillarResult captures name, outcome, detail string, elapsed seconds,
    and -- on failure -- truncated traceback (8 frames).
  - MatrixReport emits human-readable banner + optional JSON artifact
    at --json-report PATH. JSON is the machine-readable 26b gate artifact.
  - exit_code() returns 0 iff every pillar.outcome is FULL.

Step 29.y -- Cluster 8 (verify-honesty)
---------------------------------------
The original `passed: bool` outcome was binary: either the pillar's
assertions held, or they did not. That was honest as long as every
pillar always ran every assertion. P11 (async memory) and P13
(cross-tenant worker-identity) violated that invariant: when their
broker/worker probes failed, they internally fell back to a degraded
path that skipped the worker-side assertions and reported `True` --
indistinguishable in the matrix from a full-mode pass. Operators saw
"25/25 GREEN" and trusted it; in reality up to two of those greens
were known-skipped paths.

Cluster 8 introduces a tri-state outcome:

  - FULL     -- pillar ran every assertion at full mode.
  - DEGRADED -- pillar ran but skipped some assertions; a real probe
                or runtime condition forced a fallback path.
  - FAIL     -- pillar raised.

The aggregate gate fails on any DEGRADED unless the explicit
``--allow-degraded`` CLI flag is set (which CI must never pass and
which is intended only for local dev where Redis/SQS/Celery are
intentionally absent). This matches the design philosophy of
``_infra_probes.py``: the cost of a false negative (pretending to be
green when degraded) is loss of trust; the cost of a false positive
(failing the run when a known-degraded path was actually fine) is one
manual override.

Backwards compatibility: pillars whose ``run()`` returns a bare
``str`` are treated as FULL. All 22 pillars that always ran every
assertion need zero change. Only P11, P13, and P25 are modified to
return ``PillarOutcome(DEGRADED, ...)`` from their fallback branches;
P23 grows a NOT NULL gate but stays bare-str FULL.
"""

from __future__ import annotations

import enum
import json
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Union


class Outcome(str, enum.Enum):
    """Tri-state pillar outcome.

    Inheriting from ``str`` keeps JSON serialization trivial:
    ``json.dumps({"outcome": Outcome.FULL})`` emits ``"FULL"`` directly,
    and string equality (``result.outcome == "FULL"``) works for callers
    that haven't been updated to import the enum.
    """

    FULL = "FULL"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"


class PillarOutcome(NamedTuple):
    """Explicit tri-state return value for pillars that need DEGRADED.

    Pillars that always run every assertion return a bare ``str`` and
    stay FULL automatically. Pillars that have a known fallback path
    (P11, P13, P25 today) return this NamedTuple to surface the mode.

    A pillar can never return ``Outcome.FAIL`` -- failure is always
    expressed by raising an exception. ``FAIL`` exists only on the
    result side of the boundary so the matrix can encode three states
    without inventing a fourth class.
    """

    mode: Outcome  # FULL or DEGRADED only; FAIL is reserved for raised exceptions
    detail: str


# Type alias for the legal pillar return shapes.
PillarReturn = Union[str, PillarOutcome, None]


@dataclass
class PillarResult:
    """Outcome of a single pillar execution."""
    name: str
    number: int
    outcome: Outcome
    detail: str
    elapsed_s: float
    traceback_text: str | None = None

    @property
    def passed(self) -> bool:
        """Back-compat boolean: True iff outcome is FULL or DEGRADED.

        Old callers that only care about pass/fail (``if result.passed``)
        keep working. Callers that want strict full-mode pass must check
        ``result.outcome == Outcome.FULL`` explicitly. The aggregate
        ``MatrixReport.exit_code`` enforces strict-full by default; this
        property is for individual-result inspection only.
        """
        return self.outcome != Outcome.FAIL

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict() turns the enum into its repr; serialize as the
        # bare string value for JSON-readability and downstream stability.
        d["outcome"] = self.outcome.value
        # Preserve the legacy `passed` field in JSON for any external
        # consumers (CI dashboards, slack bots) reading the artifact.
        d["passed"] = self.passed
        return d


class Pillar(ABC):
    """ABC for assembly-verification pillars.

    Subclasses declare ``number`` (1-indexed for stable matrix ordering)
    and ``name`` (short human label), and implement ``run(state)`` which
    returns one of:

      - ``str``                       -- treated as FULL with that detail
      - ``PillarOutcome(mode, str)``  -- explicit FULL or DEGRADED
      - ``None``                      -- treated as FULL with detail "ok"

    Failure is always expressed by raising. The runner catches the
    exception and produces a FAIL result with a truncated traceback.
    """
    number: int
    name: str

    @abstractmethod
    def run(self, state: Any) -> PillarReturn:
        """Execute the pillar. Return success detail/outcome, or raise."""
        raise NotImplementedError


@dataclass
class MatrixReport:
    """Final matrix. Emits human banner + JSON artifact."""
    results: list[PillarResult] = field(default_factory=list)
    tenant_id: str | None = None
    base_url: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def full_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == Outcome.FULL)

    @property
    def degraded_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == Outcome.DEGRADED)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == Outcome.FAIL)

    @property
    def passed_count(self) -> int:
        """Legacy: count of pillars where outcome != FAIL.

        Kept for back-compat with the historic banner string
        ``"X/Y pillars green"``. New callers prefer ``full_count`` for
        strict-mode counting.
        """
        return self.full_count + self.degraded_count

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def all_full(self) -> bool:
        """Strict gate: every pillar ran every assertion."""
        return self.total_count > 0 and self.full_count == self.total_count

    @property
    def all_green(self) -> bool:
        """Legacy: every pillar passed (FULL or DEGRADED)."""
        return self.total_count > 0 and self.fail_count == 0

    def exit_code(self, *, allow_degraded: bool = False) -> int:
        """Return 0 iff the gate passes.

        Default (``allow_degraded=False``): every pillar must be FULL.
        Any DEGRADED or FAIL flips the exit to 1.

        With ``allow_degraded=True``: DEGRADED is permitted; only FAIL
        flips the exit. Intended for local dev where Redis/SQS/Celery
        may be intentionally absent. CI must never set this.
        """
        if self.total_count == 0:
            return 1
        if self.fail_count > 0:
            return 1
        if not allow_degraded and self.degraded_count > 0:
            return 1
        return 0

    def render_human(self) -> str:
        bar = "=" * 72
        lines = [bar, "STEP 26 VERIFICATION MATRIX", bar]
        if self.tenant_id:
            lines.append(f"tenant: {self.tenant_id}")
        if self.base_url:
            lines.append(f"base:   {self.base_url}")
        lines.append("")
        for r in self.results:
            mark = r.outcome.value
            lines.append(f"  [{mark:<8s}] {r.number:2d}. {r.name:<40s} {r.elapsed_s:6.2f}s")
            if r.outcome == Outcome.DEGRADED:
                # Always surface the degraded reason inline -- this is the
                # whole point of tri-state. An operator skimming the banner
                # must see WHY a pillar dropped to degraded mode without
                # having to dig into the JSON.
                lines.append(f"             reason: {r.detail}")
            elif r.outcome == Outcome.FAIL:
                lines.append(f"             reason: {r.detail}")
                if r.traceback_text:
                    for tb_line in r.traceback_text.rstrip().splitlines():
                        lines.append(f"             {tb_line}")
        lines.append("")
        lines.append(bar)
        if self.all_full:
            lines.append(f"RESULT: {self.full_count}/{self.total_count} FULL (all pillars at full mode)")
        elif self.all_green:
            lines.append(
                f"RESULT: {self.full_count}/{self.total_count} FULL, "
                f"{self.degraded_count} DEGRADED, 0 FAIL"
            )
        else:
            lines.append(
                f"RESULT: {self.full_count}/{self.total_count} FULL, "
                f"{self.degraded_count} DEGRADED, {self.fail_count} FAIL"
            )
        lines.append(bar)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "base_url": self.base_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            # Tri-state counts -- the new authoritative summary fields.
            "full": self.full_count,
            "degraded": self.degraded_count,
            "fail": self.fail_count,
            "total": self.total_count,
            "all_full": self.all_full,
            # Legacy fields preserved bit-for-bit for downstream consumers
            # that have not been updated to read the tri-state schema yet.
            # Any existing dashboard parsing `passed` and `all_green` keeps
            # working with the same semantics it had pre-29.y.
            "passed": self.passed_count,
            "all_green": self.all_green,
            "results": [r.to_dict() for r in self.results],
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")


class SuiteRunner:
    """Orchestrates pillar execution in declared order."""

    def __init__(self) -> None:
        self._pillars: list[Pillar] = []

    def register(self, pillar: Pillar) -> "SuiteRunner":
        self._pillars.append(pillar)
        return self

    def describe(self) -> list[tuple[int, str]]:
        return [(p.number, p.name) for p in self._pillars]

    @staticmethod
    def _normalize(ret: PillarReturn) -> tuple[Outcome, str]:
        """Coerce a pillar return value into (outcome, detail).

        Three legal shapes:
          - ``None``                       -> FULL, "ok"
          - ``str``                        -> FULL, str
          - ``PillarOutcome(mode, str)``   -> mode, str

        Anything else is a contract violation and gets coerced to FULL
        with a stringified detail to avoid losing the run, but a future
        commit could tighten this to raise.
        """
        if ret is None:
            return Outcome.FULL, "ok"
        if isinstance(ret, PillarOutcome):
            # FAIL is reserved for raised exceptions. A pillar that returns
            # FAIL via PillarOutcome is misusing the contract; coerce to
            # DEGRADED so the run still surfaces the issue without crashing.
            mode = ret.mode if ret.mode in (Outcome.FULL, Outcome.DEGRADED) else Outcome.DEGRADED
            return mode, ret.detail or "ok"
        if isinstance(ret, str):
            return Outcome.FULL, ret or "ok"
        return Outcome.FULL, str(ret)

    def run(self, state: Any, *, stop_on_fail: bool = False) -> MatrixReport:
        report = MatrixReport(started_at=time.time())
        for pillar in self._pillars:
            t0 = time.time()
            try:
                ret = pillar.run(state)
                outcome, detail = self._normalize(ret)
                report.results.append(
                    PillarResult(
                        name=pillar.name,
                        number=pillar.number,
                        outcome=outcome,
                        detail=detail,
                        elapsed_s=time.time() - t0,
                    )
                )
            except Exception as exc:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))
                report.results.append(
                    PillarResult(
                        name=pillar.name,
                        number=pillar.number,
                        outcome=Outcome.FAIL,
                        detail=f"{type(exc).__name__}: {exc}",
                        elapsed_s=time.time() - t0,
                        traceback_text=tb,
                    )
                )
                if stop_on_fail:
                    break
        report.finished_at = time.time()
        return report
