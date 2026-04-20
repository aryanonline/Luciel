"""Step 26 verification runner.

Contract:
  - Pillars declare themselves via the `Pillar` ABC.
  - SuiteRunner executes pillars in declared order (no parallelism,
    no dependency resolution -- ordering is the explicit contract).
  - Every pillar runs, even if a prior one failed (run-all-then-report).
  - PillarResult captures name, passed, detail string, elapsed seconds,
    and -- on failure -- truncated traceback (8 frames).
  - MatrixReport emits human-readable banner + optional JSON artifact
    at --json-report PATH. JSON is the machine-readable 26b gate artifact.
  - exit_code() returns 0 iff every pillar.passed is True.
"""

from __future__ import annotations

import json
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PillarResult:
    """Outcome of a single pillar execution."""
    name: str
    number: int
    passed: bool
    detail: str
    elapsed_s: float
    traceback_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pillar(ABC):
    """ABC for assembly-verification pillars.

    Subclasses declare `number` (1-indexed for stable matrix ordering) and
    `name` (short human label), and implement `run(state)` which returns a
    single-line detail string on success or raises on failure.
    """
    number: int
    name: str

    @abstractmethod
    def run(self, state: Any) -> str:
        """Execute the pillar. Return a success detail string, or raise."""
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
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def all_green(self) -> bool:
        return self.total_count > 0 and self.passed_count == self.total_count

    def exit_code(self) -> int:
        return 0 if self.all_green else 1

    def render_human(self) -> str:
        bar = "=" * 72
        lines = [bar, "STEP 26 VERIFICATION MATRIX", bar]
        if self.tenant_id:
            lines.append(f"tenant: {self.tenant_id}")
        if self.base_url:
            lines.append(f"base:   {self.base_url}")
        lines.append("")
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{mark}] {r.number:2d}. {r.name:<40s} {r.elapsed_s:6.2f}s")
            if not r.passed:
                # Surface the reason inline so the matrix is self-describing.
                lines.append(f"         reason: {r.detail}")
                if r.traceback_text:
                    for tb_line in r.traceback_text.rstrip().splitlines():
                        lines.append(f"         {tb_line}")
        lines.append("")
        lines.append(bar)
        lines.append(f"RESULT: {self.passed_count}/{self.total_count} pillars green")
        lines.append(bar)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "base_url": self.base_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed_count,
            "total": self.total_count,
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

    def run(self, state: Any, *, stop_on_fail: bool = False) -> MatrixReport:
        report = MatrixReport(started_at=time.time())
        for pillar in self._pillars:
            t0 = time.time()
            try:
                detail = pillar.run(state) or "ok"
                report.results.append(
                    PillarResult(
                        name=pillar.name,
                        number=pillar.number,
                        passed=True,
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
                        passed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                        elapsed_s=time.time() - t0,
                        traceback_text=tb,
                    )
                )
                if stop_on_fail:
                    break
        report.finished_at = time.time()
        return report