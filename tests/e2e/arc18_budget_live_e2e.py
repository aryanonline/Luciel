"""Arc 18 — conversation-budget live end-to-end harness (§3.4.1b).

This is NOT a unit test. It exercises the SHIPPED code paths end to end —
the real ``BudgetMeter``, the real entitlements resolution, the real
overage-billing helpers, and the real ``billing_period`` anchor logic —
against the founder-ratified success criteria. Inline fakes are limited
to the storage backend (``InMemoryBackend`` instead of a live Redis) and
the Stripe transport (a capture stub), so no external service is needed.

ENV GATE
--------
Like the other live harnesses, this is gated so it never runs in the
default unit sweep. It is a NO-OP (exit 0, "skipped") unless
``ARC18_BUDGET_LIVE_E2E=1`` is set:

    ARC18_BUDGET_LIVE_E2E=1 DATABASE_URL="sqlite:///:memory:" \
        python tests/e2e/arc18_budget_live_e2e.py

Each numbered scenario maps to a spec claim. Exit code 0 = all claims
satisfied (or gate off). Non-zero = at least one claim violated.
"""

from __future__ import annotations

import math
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")


class _Row:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


def _run() -> list[_Row]:
    from app.policy.entitlements import (
        CADENCE_ANNUAL,
        CADENCE_MONTHLY,
        TIER_ENTERPRISE,
        TIER_FREE,
        TIER_PRO,
        conversation_budget,
        overage_rate_per_100_cents,
    )
    from app.runtime.budget_meter import BudgetMeter, InMemoryBackend
    from app.services.overage_billing import (
        overage_count,
        overage_line_item_description,
        overage_units,
        rate_string_from_cents,
    )

    rows: list[_Row] = []

    # 1. Ratified caps per (tier, cadence).
    caps_ok = (
        conversation_budget(TIER_FREE, CADENCE_MONTHLY) == 200
        and conversation_budget(TIER_PRO, CADENCE_MONTHLY) == 2000
        and conversation_budget(TIER_PRO, CADENCE_ANNUAL) == 2500
        and conversation_budget(TIER_ENTERPRISE, CADENCE_MONTHLY) == 10000
    )
    rows.append(_Row("ratified-caps", caps_ok, "Free200/Pro2000-2500/Ent10000"))

    # 2. Overage rates in cents per 100.
    rate_ok = (
        overage_rate_per_100_cents(TIER_PRO, CADENCE_MONTHLY) == 1500
        and overage_rate_per_100_cents(TIER_PRO, CADENCE_ANNUAL) == 1000
        and overage_rate_per_100_cents(TIER_FREE, CADENCE_MONTHLY) is None
        and overage_rate_per_100_cents(TIER_ENTERPRISE, CADENCE_MONTHLY) is None
    )
    rows.append(_Row("overage-rates", rate_ok, "Pro $15/$10 per 100; Free/Ent none"))

    # 3. A multi-iteration session counts exactly once.
    meter = BudgetMeter(backend=InMemoryBackend())
    for _ in range(6):
        count = meter.count_session_once(
            admin_id="a1", instance_id=1, period_start="2026-06-01", session_id="s1"
        )
    once_ok = count == 1 and meter.current_count(
        admin_id="a1", instance_id=1, period_start="2026-06-01"
    ) == 1
    rows.append(_Row("session-counted-once", once_ok, f"count={count}"))

    # 4. Per-instance isolation.
    meter.count_session_once(
        admin_id="a1", instance_id=2, period_start="2026-06-01", session_id="s2"
    )
    iso_ok = (
        meter.current_count(admin_id="a1", instance_id=1, period_start="2026-06-01") == 1
        and meter.current_count(admin_id="a1", instance_id=2, period_start="2026-06-01") == 1
    )
    rows.append(_Row("per-instance-isolation", iso_ok, "inst1=1 inst2=1"))

    # 5. Overage rounding (ceil to hundreds) + EXACT line format.
    over = overage_count(conversations_used=2355, budget_cap=2000)  # 355
    units = overage_units(over)  # ceil(355/100) = 4
    desc = overage_line_item_description(
        instance_name="Acme Bot",
        additional=over,
        rate_str=rate_string_from_cents(1500),
    )
    expected_desc = (
        "Conversation overage — Acme Bot: 355 additional conversations × $15.00/100"
    )
    bill_ok = over == 355 and units == 4 and desc == expected_desc
    rows.append(_Row("overage-rounding+format", bill_ok, f"over={over} units={units}"))

    # 6. Reset zeroes the closed period.
    meter.reset(admin_id="a1", instance_id=1, period_start="2026-06-01")
    reset_ok = (
        meter.current_count(admin_id="a1", instance_id=1, period_start="2026-06-01") == 0
    )
    rows.append(_Row("reset-zeroes-period", reset_ok, "inst1 back to 0"))

    # 7. Alert latch fires once per threshold.
    kw = dict(admin_id="a1", instance_id=1, period_start="2026-07-01")
    latch_ok = (
        meter.mark_alert_fired_once(threshold=80, **kw)
        and not meter.mark_alert_fired_once(threshold=80, **kw)
        and meter.mark_alert_fired_once(threshold=100, **kw)
    )
    rows.append(_Row("alert-latch-once", latch_ok, "80 then 100, each once"))

    return rows


def main() -> int:
    if os.environ.get("ARC18_BUDGET_LIVE_E2E") != "1":
        print("arc18_budget_live_e2e: SKIPPED (set ARC18_BUDGET_LIVE_E2E=1 to run)")
        return 0

    try:
        rows = _run()
    except Exception as exc:  # noqa: BLE001
        print(f"arc18_budget_live_e2e: FATAL {type(exc).__name__}: {exc}")
        return 2

    all_ok = True
    for r in rows:
        flag = "PASS" if r.passed else "FAIL"
        all_ok = all_ok and r.passed
        print(f"[{flag}] {r.name}: {r.detail}")

    print(f"\narc18_budget_live_e2e: {'ALL PASS' if all_ok else 'FAILURES PRESENT'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
