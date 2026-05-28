"""
Arc 12 WU1 — entitlement cleanup regression test.

Pins:
  * ``TierEntitlement.max_composition_depth`` is GONE.
    Decision #19 ("no depth limit, no edge cap on the customer-facing
    composition graph") makes a depth field structurally wrong;
    WU1 retires it.
  * ``TierEntitlement.composition_enabled`` REMAINS and is per-tier:
    free=False, pro=True, enterprise=True (§3.3.4 master switch).
"""

from __future__ import annotations

from dataclasses import fields

import pytest


def test_tier_entitlement_no_max_composition_depth() -> None:
    """The ``max_composition_depth`` field must be absent from the
    ``TierEntitlement`` frozen dataclass. Decision #19 forbids a
    depth limit on the customer-facing composition graph; cycle
    detection + per-inbound fan-out budget (WU5) are the only
    runtime guardrails.
    """

    from app.policy.entitlements import TierEntitlement

    field_names = {f.name for f in fields(TierEntitlement)}
    assert "max_composition_depth" not in field_names, (
        "Arc 12 WU1: TierEntitlement.max_composition_depth must be "
        "RETIRED -- Decision #19 forbids a depth limit on the "
        "composition graph."
    )


def test_composition_enabled_is_per_tier_master_switch() -> None:
    """The §3.3.4 master switch survives. Free has composition
    disabled (no sibling composition on Free); Pro and Enterprise
    have it enabled."""

    from app.policy.entitlements import (
        TIER_ENTERPRISE,
        TIER_ENTITLEMENTS,
        TIER_FREE,
        TIER_PRO,
    )

    assert TIER_ENTITLEMENTS[TIER_FREE].composition_enabled is False, (
        "Free must not have composition enabled (§3.3.4 + Vision §7)."
    )
    assert TIER_ENTITLEMENTS[TIER_PRO].composition_enabled is True, (
        "Pro must have composition enabled (§3.3.4 + Vision §7)."
    )
    assert TIER_ENTITLEMENTS[TIER_ENTERPRISE].composition_enabled is True, (
        "Enterprise must have composition enabled (§3.3.4 + Vision §7)."
    )


def test_no_callsite_references_max_composition_depth() -> None:
    """A grep-style sanity check across app/ and tests/ that no
    surviving code references ``max_composition_depth``. The
    Alembic history file is excluded (it created the column that
    backed the now-retired field; the DB drop lands with the Arc 12
    schema sweep)."""

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    # Files allowed to mention the retired field by name: this very
    # test file (its assertion is the mention) and the dataclass
    # source where the retirement comment documents the decision.
    self_path = pathlib.Path(__file__).resolve()
    allowed_basenames = {
        "entitlements.py",
        "test_arc12_wu1_entitlements.py",
        # Arc 12 WU2 asserts the column drop in its migration-shape
        # test — mentioning the name is the assertion's substance.
        "test_arc12_wu2_authorization.py",
    }

    offenders: list[str] = []
    for sub in ("app", "tests"):
        for path in (root / sub).rglob("*.py"):
            if path.resolve() == self_path:
                continue
            if path.name in allowed_basenames:
                continue
            text = path.read_text(encoding="utf-8")
            if "max_composition_depth" in text:
                offenders.append(str(path.relative_to(root)))

    assert not offenders, (
        f"Arc 12 WU1: 'max_composition_depth' still referenced in "
        f"{offenders!r} -- WU1 retires the field; remove all reads."
    )
