"""
Default retention periods per data category.

These are seeded as platform-wide defaults (tenant_id = NULL).
Tenants can override any of these through the admin API.

Retention periods are based on PIPEDA Principle 4.5.3:
personal information no longer required to fulfil the identified
purposes should be destroyed, erased, or made anonymous.
"""

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
        "data_category": "knowledge_embeddings",
        "retention_days": 0,
        "action": "delete",
        "purpose": "Domain knowledge, not end-user PII. No automatic purge. "
                   "Managed manually through knowledge ingestion API.",
    },
]