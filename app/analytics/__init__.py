"""§3.9 Analytics & Reporting subsystem (Unit 13d).

This is the §8 doctrine path ``app/analytics/`` — previously NO-MODULE-YET,
now MATCHES-DOC. The subsystem is READ-ONLY: it computes aggregate
metrics over the EXISTING tenant-scoped stores (sessions, leads,
escalation_events, traces, the admin_audit_log, and the Redis budget
meter). It introduces NO new tables, NO new write path, and NO new PII —
every query is an aggregate scoped ``WHERE admin_id = :admin_id`` and runs
through the RLS-bound TenantScoped session, so a tenant's analytics can
never include another tenant's data.

The single public surface is :class:`~app.analytics.service.AnalyticsService`.
``app/api/v1/analytics.py`` adapts it to HTTP (tier-shaped JSON + the Pro
CSV export); ``app/api/v1/admin/usage.py`` remains the budget-only usage
panel and is reused (not duplicated) for the utilization metric.
"""
from __future__ import annotations

from app.analytics.service import (
    AnalyticsPeriod,
    AnalyticsService,
    BASIC_METRIC_KEYS,
    PRO_ONLY_METRIC_KEYS,
)

__all__ = [
    "AnalyticsService",
    "AnalyticsPeriod",
    "BASIC_METRIC_KEYS",
    "PRO_ONLY_METRIC_KEYS",
]
