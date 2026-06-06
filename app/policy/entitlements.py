"""Tier-entitlement matrix — Free/Pro only.

The deferred Enterprise tier (and the v1 4-tier surface before it) has been
excised in the audit-and-alignment phase (Unit 1). The platform ships exactly
two tiers — Free and Pro — per the ratified product docs:

* Vision §7 (tier table: Free / Pro Monthly / Pro Annual),
* Locked Decision #12 (one Luciel per account),
* Locked Decision #19 (single-login; no team seats, no custom roles),
* Locked Decision #35 + Open Decisions #7/#8 (multi-Luciel & Enterprise DEFERRED),
* Architecture §3.7.1 (single-login model), §6 (deferred list).

Enterprise-only machinery removed in this unit: the TIER_ENTERPRISE row, the
``admin_tier_overrides`` override hook on ``resolve_entitlement`` (Enterprise
was the only tier that carried override rows), the SSO / custom-role-authoring /
sibling-composition / knowledge-share-grant / cross-instance-federation /
delegated-admin / multi-seat axes, the dashboard rollup views, the
escalation-chain and CSM-alert helpers, and the SMS brokerage-routing flag.

Public surface (unchanged names; consumed across the codebase):
  * TIER_FREE / TIER_PRO / ALL_TIERS_V2
  * TierEntitlement dataclass + TIER_ENTITLEMENTS map
  * resolve_entitlement(tier, axis) — static fail-closed lookup
  * get_tier_entitlement(tier)
  * per_instance_api_rate_limit_rpm / per_key_api_rate_limit_rpm
  * channels_available / sms_dedicated_number_entitled
  * business_context_max_chars / custom_personality_enabled / lead_routing_enabled
  * escalation_notify_channels / escalation_secondary_contact_enabled
  * conversation_budget / overage_rate_per_100_cents / overage_price_config_key
  * budget_overage_billed / budget_alert_channels

NOTE: the Pro conversation-budget + overage VALUES below remain at their
pre-existing (drifted) figures (2000/2500, $15/$10) in this unit; they are
corrected to the ratified Locked Decision #15 values (1000/1200, $35/$30) in
Unit 2 (Tier-matrix alignment), to keep the deferred-feature excision and the
value-reconciliation in separate, individually-verifiable units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Tier constants. String literals match the DB CHECK ('free','pro').
TIER_FREE = "free"
TIER_PRO = "pro"
ALL_TIERS_V2: tuple[str, ...] = (TIER_FREE, TIER_PRO)

# Support-SLA literal labels. Free has no SLA; Pro is 48h email.
SUPPORT_SLA_COMMUNITY = "community"
SUPPORT_SLA_EMAIL_48H = "email_48h"


@dataclass(frozen=True)
class TierEntitlement:
    """One tier's complete entitlement row (Free/Pro)."""

    # Axis 1 -- Model tier per Instance ("base" / "mid")
    model_tier_default: str

    # Axis 2 -- API access
    api_enabled: bool
    api_rate_limit_rpm: int
    embed_key_count_cap: int | None

    # Axis 3 -- Audit retention (days)
    audit_retention_days: int | None

    # Axis 4 -- Knowledge byte cap per Admin (Vision §7: Free 100 MB, Pro 5 GB)
    knowledge_bytes_cap: int | None
    # Per-file knowledge byte cap (Vision §3.3: Free 10 MB, Pro 50 MB)
    knowledge_per_file_bytes_cap: int
    # Website crawl ingestion (Vision §3.3: Pro only)
    knowledge_website_crawl_enabled: bool
    # Graph knowledge store (Vision §7: Pro only — Architecture §3.2.1)
    knowledge_graph_enabled: bool

    # Axis 5 -- Custom widget branding + custom-domain CNAME (Pro)
    widget_branding_custom: bool
    widget_custom_domain_cname_cap: int | None

    # Axis 6 -- Webhook outbound
    webhook_outbound_enabled: bool

    # Axis 7 -- SLA
    uptime_sla_pct: float | None
    support_sla: str

    # Axis 8 -- Data residency
    data_residency_region: str

    # Axis 9 -- Export
    export_csv_enabled: bool
    export_audit_chain_enabled: bool

    # Stripe customer record requirement (Free has NULL until upgrade)
    stripe_customer_record_required: bool


# Per-tier entitlement map. Values mirror Vision §7. When this map and any
# engineering spec diverge, **this map wins** and the spec is corrected.
TIER_ENTITLEMENTS: dict[str, TierEntitlement] = {
    TIER_FREE: TierEntitlement(
        model_tier_default="base",
        api_enabled=True,
        api_rate_limit_rpm=30,
        embed_key_count_cap=1,
        audit_retention_days=30,
        knowledge_bytes_cap=100 * 1024 * 1024,        # 100 MB (Vision §7)
        knowledge_per_file_bytes_cap=10 * 1024 * 1024,  # 10 MB
        knowledge_website_crawl_enabled=False,
        knowledge_graph_enabled=False,
        widget_branding_custom=False,
        widget_custom_domain_cname_cap=0,
        webhook_outbound_enabled=False,
        uptime_sla_pct=None,                          # best-effort
        support_sla=SUPPORT_SLA_COMMUNITY,
        data_residency_region="ca-central-1",
        export_csv_enabled=False,
        export_audit_chain_enabled=False,
        stripe_customer_record_required=False,        # NULL until upgrade
    ),
    TIER_PRO: TierEntitlement(
        model_tier_default="mid",
        api_enabled=True,
        api_rate_limit_rpm=300,
        embed_key_count_cap=10,
        audit_retention_days=365,                     # 1 year (Vision §7)
        knowledge_bytes_cap=5 * 1024 * 1024 * 1024,   # 5 GB (Vision §7)
        knowledge_per_file_bytes_cap=50 * 1024 * 1024,  # 50 MB
        knowledge_website_crawl_enabled=True,
        knowledge_graph_enabled=True,
        widget_branding_custom=False,
        widget_custom_domain_cname_cap=1,             # e.g. chat.theircompany.com
        webhook_outbound_enabled=True,
        uptime_sla_pct=99.9,                          # Vision §7 / §5.6
        support_sla=SUPPORT_SLA_EMAIL_48H,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=False,
        stripe_customer_record_required=True,
    ),
}

assert set(TIER_ENTITLEMENTS.keys()) == set(ALL_TIERS_V2), (
    "TIER_ENTITLEMENTS keys must equal ALL_TIERS_V2 (free, pro)."
)


def resolve_entitlement(*, tier: str, axis: str, overrides: dict[str, Any] | None = None) -> Any:
    """Resolve a single entitlement value for an Admin.

    Static fail-closed lookup: ``TIER_ENTITLEMENTS[tier].<axis>``. The
    Enterprise-only ``admin_tier_overrides`` hook was removed with the
    Enterprise tier (Unit 1); the ``overrides`` parameter is retained for
    signature compatibility with existing call sites but is ignored — Free
    and Pro never carry override rows.

    Raises ``AttributeError`` for an unknown axis (fail-closed; no permissive
    default) and ``KeyError`` for an unknown tier — callers should validate
    ``tier in ALL_TIERS_V2`` first.
    """
    return getattr(TIER_ENTITLEMENTS[tier], axis)


def get_tier_entitlement(tier: str) -> TierEntitlement:
    """Return the full ``TierEntitlement`` row for a tier."""
    return TIER_ENTITLEMENTS[tier]


# ---------------------------------------------------------------------
# Per-bucket rate-limit derivations (functions, not frozen fields).
# ---------------------------------------------------------------------

def per_instance_api_rate_limit_rpm(*, tier: str, overrides: dict[str, Any] | None = None) -> int:
    """Per-Instance rpm bucket cap. One Luciel per account (Locked Dec #12),
    so this equals the per-Admin rpm; retained for call-site compatibility."""
    return int(resolve_entitlement(tier=tier, axis="api_rate_limit_rpm"))


def per_key_api_rate_limit_rpm(*, tier: str, overrides: dict[str, Any] | None = None) -> int:
    """Per-embed-key rpm bucket cap. Floor-divides ``api_rate_limit_rpm`` by
    ``embed_key_count_cap`` so a leaked/buggy key cannot burn the whole
    allotment; 1rpm floor. ``None`` cap returns the full rpm."""
    rpm = resolve_entitlement(tier=tier, axis="api_rate_limit_rpm")
    cap = resolve_entitlement(tier=tier, axis="embed_key_count_cap")
    if cap is None or int(cap) <= 0:
        return int(rpm)
    return max(1, int(rpm) // int(cap))


# ---------------------------------------------------------------------
# Channel-availability derivations (Vision §7).
#   Free -> {widget};  Pro -> {widget, email, sms}
# (Voice + Meta + Slack are Pro channels gated by adapter readiness in
# their own arcs; this is the tier floor.)
# ---------------------------------------------------------------------

CHANNEL_WIDGET = "widget"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

_CHANNELS_BY_TIER: dict[str, frozenset[str]] = {
    TIER_FREE: frozenset({CHANNEL_WIDGET}),
    TIER_PRO: frozenset({CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS}),
}


def channels_available(tier: str) -> frozenset[str]:
    """Channel ids a tier may enable. Fail-closed: unknown tier → Free set."""
    return _CHANNELS_BY_TIER.get(tier, _CHANNELS_BY_TIER[TIER_FREE])


def sms_dedicated_number_entitled(tier: str) -> bool:
    """Whether a tier gets a dedicated SMS number per Instance (Pro only,
    and only when SMS is in the tier's channel set)."""
    if CHANNEL_SMS not in channels_available(tier):
        return False
    return tier == TIER_PRO


# ---------------------------------------------------------------------
# Instance-config-pillar derivations (Vision §3.5 / §3.4).
# ---------------------------------------------------------------------

_BUSINESS_CONTEXT_CAP_BY_TIER: dict[str, int] = {
    TIER_FREE: 280,
    TIER_PRO: 280,
}


def business_context_max_chars(tier: str) -> int:
    """Max ``business_context`` length (Vision §3.5: 280 chars, all tiers).
    Fail-closed to the Free cap for unknown tiers."""
    return _BUSINESS_CONTEXT_CAP_BY_TIER.get(tier, _BUSINESS_CONTEXT_CAP_BY_TIER[TIER_FREE])


def custom_personality_enabled(tier: str) -> bool:
    """Whether a tier may use the ``custom`` personality preset (Pro only —
    Vision §3.5). Named presets are available on every tier."""
    return tier == TIER_PRO


def lead_routing_enabled(tier: str) -> bool:
    """Whether a tier may configure per-signal lead routing (Pro only —
    Vision §3.4; Free is single email contact)."""
    return tier == TIER_PRO


# Admin-notification channels for escalation contacts, per tier (Vision §3.4):
#   Free = email;  Pro = email + sms + slack
ESCALATION_NOTIFY_EMAIL = "email"
ESCALATION_NOTIFY_SMS = "sms"
ESCALATION_NOTIFY_SLACK = "slack"

_ESCALATION_NOTIFY_CHANNELS_BY_TIER: dict[str, frozenset[str]] = {
    TIER_FREE: frozenset({ESCALATION_NOTIFY_EMAIL}),
    TIER_PRO: frozenset(
        {ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS, ESCALATION_NOTIFY_SLACK}
    ),
}


def escalation_notify_channels(tier: str) -> frozenset[str]:
    """Admin-notification channels a tier may route escalation contacts
    through (Vision §3.4). Fail-closed: unknown tier → Free set."""
    return _ESCALATION_NOTIFY_CHANNELS_BY_TIER.get(
        tier, _ESCALATION_NOTIFY_CHANNELS_BY_TIER[TIER_FREE]
    )


def escalation_secondary_contact_enabled(tier: str) -> bool:
    """Whether a tier may configure a secondary escalation contact +
    per-signal routing rules (Pro only; Free is primary email only)."""
    return tier == TIER_PRO


# ---------------------------------------------------------------------
# Conversation-budget + overage axis (Vision §7; Architecture §3.4.1b).
#
# Per-(tier, cadence) since Vision §7 treats Pro Monthly and Pro Annual as
# distinct budget/rate rows. Cadence lives on subscriptions.billing_cadence.
# Money is in CENTS to avoid float drift.
#
# NOTE: the Pro values here are the pre-existing (drifted) figures; Unit 2
# corrects them to Locked Decision #15 (1000/1200 conv, $35/$30 per 100).
# ---------------------------------------------------------------------

CADENCE_MONTHLY = "monthly"
CADENCE_ANNUAL = "annual"

_CONVERSATION_BUDGET: dict[tuple[str, str], int] = {
    (TIER_FREE, CADENCE_MONTHLY): 200,
    (TIER_FREE, CADENCE_ANNUAL): 200,
    (TIER_PRO, CADENCE_MONTHLY): 2000,
    (TIER_PRO, CADENCE_ANNUAL): 2500,
}

_OVERAGE_RATE_PER_100_CENTS: dict[tuple[str, str], int | None] = {
    (TIER_FREE, CADENCE_MONTHLY): None,
    (TIER_FREE, CADENCE_ANNUAL): None,
    (TIER_PRO, CADENCE_MONTHLY): 1500,
    (TIER_PRO, CADENCE_ANNUAL): 1000,
}

_OVERAGE_PRICE_CONFIG_KEY: dict[tuple[str, str], str | None] = {
    (TIER_FREE, CADENCE_MONTHLY): None,
    (TIER_FREE, CADENCE_ANNUAL): None,
    (TIER_PRO, CADENCE_MONTHLY): "stripe_price_overage_pro_monthly",
    (TIER_PRO, CADENCE_ANNUAL): "stripe_price_overage_pro_annual",
}


def _norm_cadence(cadence: str | None) -> str:
    """Fail-closed cadence normalisation. Unknown/None → monthly."""
    return CADENCE_ANNUAL if cadence == CADENCE_ANNUAL else CADENCE_MONTHLY


def conversation_budget(tier: str, cadence: str | None = None) -> int:
    """Per-instance conversation budget for a (tier, cadence). Fail-closed to
    the Free cap (200) for an unknown tier."""
    key = (tier, _norm_cadence(cadence))
    if key not in _CONVERSATION_BUDGET:
        return _CONVERSATION_BUDGET[(TIER_FREE, CADENCE_MONTHLY)]
    return _CONVERSATION_BUDGET[key]


def overage_rate_per_100_cents(tier: str, cadence: str | None = None) -> int | None:
    """Overage rate in cents per additional 100 conversations. None = no
    platform overage billing (Free graceful cap). Fail-closed to None."""
    return _OVERAGE_RATE_PER_100_CENTS.get((tier, _norm_cadence(cadence)))


def overage_price_config_key(tier: str, cadence: str | None = None) -> str | None:
    """Settings attribute name holding the Stripe metered overage Price id for
    a (tier, cadence), or None when no fixed platform price applies (Free)."""
    return _OVERAGE_PRICE_CONFIG_KEY.get((tier, _norm_cadence(cadence)))


def budget_overage_billed(tier: str) -> bool:
    """Whether exceeding budget produces an overage charge (Pro) rather than a
    graceful cap (Free). Fail-closed: unknown → False."""
    return tier == TIER_PRO


# Budget alert thresholds (percent of cap) and the channels that fire at each
# (Vision §7 / §3.4.1b). Free: only the budget_exhausted escalation at 100% on
# email. Pro: 80% email, 100% email+SMS.
ALERT_THRESHOLD_80 = 80
ALERT_THRESHOLD_100 = 100

_BUDGET_ALERT_CHANNELS: dict[tuple[str, int], frozenset[str]] = {
    (TIER_FREE, ALERT_THRESHOLD_80): frozenset(),
    (TIER_FREE, ALERT_THRESHOLD_100): frozenset({ESCALATION_NOTIFY_EMAIL}),
    (TIER_PRO, ALERT_THRESHOLD_80): frozenset({ESCALATION_NOTIFY_EMAIL}),
    (TIER_PRO, ALERT_THRESHOLD_100): frozenset(
        {ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS}
    ),
}


def budget_alert_channels(tier: str, threshold_pct: int) -> frozenset[str]:
    """Notification channels that fire for a tier at a budget threshold (80 or
    100). Fail-closed: unknown (tier, threshold) → empty set."""
    return _BUDGET_ALERT_CHANNELS.get((tier, threshold_pct), frozenset())
