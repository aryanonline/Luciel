"""
Default retention periods per data category.

These are seeded as platform-wide defaults (admin_id = NULL).
Tenants can override any of these through the admin API.

Retention periods are based on PIPEDA Principle 4.5.3:
personal information no longer required to fulfil the identified
purposes should be destroyed, erased, or made anonymous.

RESCAN TIER-DE(ent) — Per-tier retention (Architecture §3.4.10)
---------------------------------------------------------------
Architecture §3.4.10 specifies per-tier retention for transcripts
(sessions/messages) and summaries:

    Tier        Transcript (sessions/messages)   Summary
    -----       ----------------------------     -------
    Free        30 days                          90 days
    Pro         1 year (365 days)               1 year (365 days)
    Enterprise  7 years (2555 days)             7 years (2555 days)

Resolution order (priority high to low):
    1. Tenant override  — a RetentionPolicy row with admin_id = <admin>
       (explicit per-tenant configuration set by the tenant admin via
       the retention API). This represents a contractual or compliance
       customisation and always wins.
    2. Tier default  — the TIER_RETENTION_DEFAULTS value for the
       tenant's current billing tier. This is the new layer introduced
       by RESCAN TIER-DE(ent); it replaces the flat 730-day platform
       default as the operative value for transcript/summary categories
       when no tenant override exists.
    3. Platform default  — the RetentionPolicy row with admin_id = NULL
       seeded from PLATFORM_DEFAULTS below. This acts as the catch-all
       for categories not covered by a tier-default (e.g.
       memory_items, traces, knowledge_chunks) and for any future
       category added before its tier-defaults are specified.

Note on audit log retention: the admin_audit_logs retention is handled
by the separate AuditRetentionService (app/services/audit_retention_service.py)
which already implements tier-conditional windows (30d/1y/7y). That
service is NOT touched by this change.

Note on S3 cold-archive (Architecture §3.4.10, "conversations archived
to S3 cold storage after 90 days"): this is a REMAINING GAP. Cold-
archiving conversations to S3 requires infra provisioning (S3 bucket,
IAM role, archiver worker). It is NOT implemented by this unit.
Status: BLOCKED-EXTERNAL / defer to infra phase.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-tier retention defaults (Architecture §3.4.10)
# ---------------------------------------------------------------------------
# Key: tier string ("free" / "pro" / "enterprise")
# Value: dict mapping data_category -> retention_days
#
# Categories covered:
#   "sessions"  = full conversation transcript (sessions table)
#   "messages"  = full conversation transcript (messages table, same window)
#   ("summary" is not a standalone DB category in the current schema;
#    the 90d Free / 365d Pro+Enterprise summary window is documented here
#    for completeness and can be operationalised when a summaries table
#    is added. The sessions/messages window implements the transcript SLA.)
#
# 7 years = 365 * 7 = 2555 days.

TIER_RETENTION_DEFAULTS: dict[str, dict[str, int]] = {
    "free": {
        "sessions": 30,
        "messages": 30,
        # summary window — when a summaries table exists, seed 90 days for Free
        # "summaries": 90,
    },
    "pro": {
        "sessions": 365,
        "messages": 365,
        # summary window same as transcript for Pro
        # "summaries": 365,
    },
    # Enterprise tier deferred (Open Decision #8); removed in Unit 1.
}

# Summary retention (days) by tier — Architecture §3.4.10.
# Exposed separately so tests can assert the exact summary values without
# needing a "summaries" category row in the DB.
TIER_SUMMARY_RETENTION_DAYS: dict[str, int] = {
    "free":       90,
    "pro":        365,
    # Enterprise tier deferred (Open Decision #8); removed in Unit 1.
}

# Transcript category names (sessions + messages share the transcript window).
TRANSCRIPT_CATEGORIES: frozenset[str] = frozenset({"sessions", "messages"})


def resolve_retention_days(
    *,
    data_category: str,
    tier: str | None,
    tenant_override_days: int | None = None,
    platform_default_days: int | None = None,
) -> int | None:
    """Resolve the effective retention_days for a (category, tier) tuple.

    Resolution order (high to low priority):
    1. tenant_override_days  — explicit per-tenant value, if present.
    2. Tier default          — TIER_RETENTION_DEFAULTS[tier][category],
                               if the tier is known and the category has
                               a tier-default entry.
    3. platform_default_days — the platform-wide seed value, if given.

    Returns None if no layer yields a value (caller should treat as
    "no policy / no auto-purge").

    Parameters
    ----------
    data_category:
        The data category key (e.g. "sessions", "messages").
    tier:
        The admin's current billing tier ("free" / "pro" / "enterprise").
        Pass None to skip the tier-default layer.
    tenant_override_days:
        The retention_days from the tenant-specific RetentionPolicy row,
        or None if no such row exists.
    platform_default_days:
        The retention_days from the platform-default RetentionPolicy row
        (admin_id IS NULL), or None if not available.
    """
    # Layer 1: tenant override wins unconditionally.
    if tenant_override_days is not None:
        return tenant_override_days

    # Layer 2: tier default (new layer, RESCAN TIER-DE(ent)).
    if tier is not None:
        tier_cat_map = TIER_RETENTION_DEFAULTS.get(tier, {})
        tier_days = tier_cat_map.get(data_category)
        if tier_days is not None:
            return tier_days

    # Layer 3: platform default.
    return platform_default_days


PLATFORM_DEFAULTS = [
    {
        "data_category": "sessions",
        "retention_days": 730,
        "action": "anonymize",
        "purpose": "PIPEDA 4.5.2 — retain session metadata for 2 years "
                   "to allow individual access requests after decisions. "
                   "User identity is anonymized, session structure is preserved.",
    },
    {
        "data_category": "messages",
        "retention_days": 730,
        "action": "anonymize",
        "purpose": "Messages are tied to session lifecycle. Content is redacted "
                   "after 2 years to preserve conversation structure for analytics "
                   "without retaining PII.",
    },
    {
        "data_category": "memory_items",
        "retention_days": 365,
        "action": "anonymize",
        "purpose": "Personal facts about users. Anonymized after 1 year. "
                   "Users can request earlier deletion via subject access request.",
    },
    {
        "data_category": "traces",
        "retention_days": 365,
        "action": "delete",
        "purpose": "Operational telemetry with minimal PII. Deleted after 1 year. "
                   "Aggregated metrics are preserved separately.",
    },
    {
        # Cleanup A: renamed from "knowledge_embeddings" to
        # "knowledge_chunks". Paired alembic migration updates
        # persisted rows.
        "data_category": "knowledge_chunks",
        "retention_days": 0,
        "action": "delete",
        "purpose": "Domain knowledge, not end-user PII. No automatic purge. "
                   "Managed manually through knowledge ingestion API.",
    },
]