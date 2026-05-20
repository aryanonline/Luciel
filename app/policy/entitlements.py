"""Tier-entitlement matrix v1 (Step 30a.6, 2026-05-20).

The operational differences between Individual, Team, and Company tiers
-- beyond instance cap and price -- live in this module as the single
first-class artifact. CANONICAL_RECAP \u00a714 "Entitlement matrix" is the
buyer-facing surface of this same shape; Pricing.tsx feature lists are
derived from this same shape. When the three surfaces disagree, this
policy module wins and the disagreement is recorded as a drift in
DRIFTS \u00a73.

The matrix surfaces 18 named dimensions (the channel-adapter row splits
cleanly into Voice / SMS / Email rather than collapsing into one).
Eight of those 18 are deferred to follow-up Steps -- they carry
``enforced=False`` plus a ``pairing_step`` token. The deferral umbrella
is tracked under
``D-entitlement-matrix-v1-roadmap-rows-deferred-2026-05-20`` in DRIFTS
\u00a73; per-row drifts open lazily at the corresponding-Step touch (e.g.
the Step 34a commit opens three per-row drifts for Voice / SMS / Email
channel-adapter enforcement, not before).

See also:
  * CANONICAL_RECAP \u00a712 Step 30a.6 row -- the operational entry point.
  * CANONICAL_RECAP \u00a714 "Entitlement matrix" sub-section -- the
    buyer-facing table generated against this same shape.
  * DRIFTS `D-tier-semantics-realignment-2026-05-20` -- umbrella drift.
  * DRIFTS `D-entitlement-matrix-v1-2026-05-20` -- this module's drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.subscription import TIER_COMPANY, TIER_INDIVIDUAL, TIER_TEAM


# ---------------------------------------------------------------------
# Dimension + Entitlement shapes
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """One cell of the matrix -- a per-tier, per-dimension entitlement.

    Attributes:
      value: The Live-Today value -- an int (cap), bool (yes/no), str
        (literal label like "Email response within 24h"), or ``None``
        when the cell is N/A for that tier.
      enforced: ``True`` when the runtime call-site actually enforces
        this value today; ``False`` when the value is committed to the
        buyer but enforcement is deferred to ``pairing_step``.
      pairing_step: The follow-up Step token where deferred enforcement
        will land. ``None`` when ``enforced=True``.
    """

    value: Any
    enforced: bool
    pairing_step: str | None = None


@dataclass(frozen=True)
class Dimension:
    """One row of the matrix -- a named operational dimension."""

    key: str
    label: str


# ---------------------------------------------------------------------
# Dimensions (the 18 rows of the matrix, in CANONICAL_RECAP \u00a714 order)
# ---------------------------------------------------------------------

DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("seats", "Seats (people who can sign in under the tenant)"),
    Dimension("luciel_instances_cap", "Luciel instances cap"),
    Dimension("domains_cap", "Domains cap"),
    Dimension("leads_cap", "Leads / conversations stored cap"),
    Dimension("voice_channel", "Voice channel adapter"),
    Dimension("sms_channel", "SMS channel adapter"),
    Dimension("email_channel", "Email channel adapter"),
    Dimension("widget_cap", "Widget (embeddable chat)"),
    Dimension("conversations_per_day_per_seat", "Conversations per day, per seat"),
    Dimension("api_rate_limit_rpm", "API rate limit (requests per minute, per tenant)"),
    Dimension("concurrent_instances", "Concurrent Sarah instances (per-tenant LLM concurrency ceiling)"),
    Dimension("cross_domain_memory", "Cross-domain memory"),
    Dimension("audit_retention_days", "Audit retention"),
    Dimension("audit_csv_export", "Audit CSV export"),
    Dimension("custom_branding", "Custom widget branding (operator-supplied theme)"),
    Dimension("sso", "SSO (SAML / OIDC enterprise identity)"),
    Dimension("priority_support", "Priority support"),
    Dimension("dedicated_success_manager", "Dedicated success manager"),
)

assert len(DIMENSIONS) == 18, (
    "DIMENSIONS must remain 18 rows -- see CANONICAL_RECAP \u00a714 "
    "Entitlement matrix and DRIFTS `D-entitlement-matrix-v1-2026-05-20`."
)


# Pairing-step tokens for the eight deferred (Roadmap) rows.
_STEP_34A_CHANNELS = "Step 34a (channel adapter framework)"
_STEP_31X_METERING = "Step 31.x (per-seat metering + per-tier rate-limit profiles)"
_STEP_36_COUNCIL = "Step 36 (Luciel Council) / Step 31.x (concurrency counter)"
_STEP_37_HYBRID = "Step 37 (hybrid retrieval) -- tier-gated cross-Domain"
_STEP_NEXT_CRON = "next cron touch -- per-tier retention class into the purge worker"
_STEP_AUDIT_EXPORT = "Step 31.x (audit CSV export route)"
_STEP_WIDGET_THEME = "next widget touch -- tier-gating the theme field"
_STEP_FUTURE_OPS = "future ops step -- SLA infrastructure"
_STEP_FIRST_COMPANY_ANNUAL = "first Company annual hand-off"
_STEP_BEYOND_33B = "future enterprise step beyond Step 33b -- SSO integration"


# ---------------------------------------------------------------------
# EntitlementSet -- one tier's full row of values
# ---------------------------------------------------------------------


EntitlementSet = dict[str, Entitlement]


def _individual_set() -> EntitlementSet:
    return {
        "seats": Entitlement(1, enforced=True),
        "luciel_instances_cap": Entitlement(3, enforced=True),
        "domains_cap": Entitlement(0, enforced=True),
        "leads_cap": Entitlement(3, enforced=True),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(1, enforced=True),
        "conversations_per_day_per_seat": Entitlement(50, enforced=False, pairing_step=_STEP_31X_METERING),
        "api_rate_limit_rpm": Entitlement(10, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(2, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(None, enforced=True),  # N/A -- no second domain to cross
        "audit_retention_days": Entitlement(30, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(False, enforced=True),
        "custom_branding": Entitlement(False, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement(False, enforced=True),
        "priority_support": Entitlement("None (community / docs)", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(False, enforced=True),
    }


def _team_set() -> EntitlementSet:
    return {
        "seats": Entitlement(10, enforced=True),
        "luciel_instances_cap": Entitlement(10, enforced=True),
        "domains_cap": Entitlement(0, enforced=True),  # Step 30a.6: Team is flat, no Domain layer
        "leads_cap": Entitlement(100, enforced=True),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(3, enforced=True),
        "conversations_per_day_per_seat": Entitlement(200, enforced=False, pairing_step=_STEP_31X_METERING),
        "api_rate_limit_rpm": Entitlement(60, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(10, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(None, enforced=True),  # N/A -- no Domain layer at Team
        "audit_retention_days": Entitlement(90, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(False, enforced=True),
        "custom_branding": Entitlement(False, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement(False, enforced=True),
        "priority_support": Entitlement("Email response within 24h", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(False, enforced=True),
    }


def _company_set() -> EntitlementSet:
    return {
        "seats": Entitlement(50, enforced=True),
        "luciel_instances_cap": Entitlement(50, enforced=True),
        "domains_cap": Entitlement(50, enforced=True),
        "leads_cap": Entitlement(None, enforced=True),  # Unlimited
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(None, enforced=True),  # Unlimited (derived from instance cap)
        "conversations_per_day_per_seat": Entitlement(None, enforced=False, pairing_step=_STEP_31X_METERING),  # Unlimited
        "api_rate_limit_rpm": Entitlement(300, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(50, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(True, enforced=False, pairing_step=_STEP_37_HYBRID),
        "audit_retention_days": Entitlement(365, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(True, enforced=False, pairing_step=_STEP_AUDIT_EXPORT),
        "custom_branding": Entitlement(True, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_BEYOND_33B),
        "priority_support": Entitlement("Email + Slack within 4h", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement("Annual cadence only", enforced=False, pairing_step=_STEP_FIRST_COMPANY_ANNUAL),
    }


ENTITLEMENTS_BY_TIER: dict[str, EntitlementSet] = {
    TIER_INDIVIDUAL: _individual_set(),
    TIER_TEAM: _team_set(),
    TIER_COMPANY: _company_set(),
}


# ---------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------


def get_entitlement(*, tier: str, dimension_key: str) -> Entitlement:
    """Look up a single entitlement cell.

    Raises ``KeyError`` if either the tier or the dimension is unknown
    -- callers should validate inputs against ``TIER_*`` constants and
    ``DIMENSIONS`` before calling.
    """
    tier_set = ENTITLEMENTS_BY_TIER[tier]
    return tier_set[dimension_key]


def is_enforced(*, tier: str, dimension_key: str) -> bool:
    """``True`` when the runtime call-site enforces this dimension today.

    Used by call-sites that want to skip a check when the matrix has
    declared a value but enforcement is deferred to a follow-up Step.
    """
    return get_entitlement(tier=tier, dimension_key=dimension_key).enforced
