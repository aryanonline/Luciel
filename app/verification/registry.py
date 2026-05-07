"""Single source of truth for pillar registration order.

Step 29 Commit D extracted the pillar-list literal from
``app/verification/__main__.py`` into this module so the existing
``python -m app.verification`` entry point AND the new pytest harness in
``tests/verification/test_pillars.py`` reference the SAME list. Without
this extraction, the two entry points would each carry their own
PRE_TEARDOWN_PILLARS literal and could drift apart silently -- the
classic B.1-shaped duplication risk that C.6 just finished cleaning up
for ``_broker_reachable`` / ``_worker_reachable``. The lesson there
applies here: if the same list lives in two places, it WILL drift.

Why a function and not a module-level list? Two reasons:

  1. **Lazy import.** Each pillar module imports its dependencies at
     module load time (httpx for HTTP pillars, sqlalchemy for the
     schema-integrity pillars, redis for P11/P13's mode probes). Eagerly
     importing all 23 pillars whenever any caller does
     ``from app.verification.registry import ...`` would forcibly load
     the full pillar dependency graph even for callers that only want
     the teardown-integrity pillar. A function lets each caller pay the
     import cost only when it actually needs the list.
  2. **Future variants.** A future step may want a "smoke" subset (P1,
     P2, P15-only) for fast CI, or a "platform-admin-only" subset
     (P19-P23) for security regression checks. Exposing a function
     makes it trivial to add ``smoke_pillars()`` or
     ``security_pillars()`` alongside the canonical full list without
     changing existing callers.

Ordering is the explicit contract -- preserved verbatim from the
landed list at commit 85a29f3 and updated through Step 28 Phase 2 +
Step 29 Commits B/C. ``SuiteRunner`` runs pillars in registration order
with no parallelism; reordering this list reorders the verify matrix.

The pillar number (P1.number = 1, P11.number = 11, ...) is independent
of registration order -- it's the stable matrix-row index that the
JSON report uses for cross-run comparison. Registration order is the
EXECUTION order; pillar number is the REPORTING order. They happen to
match today, but the runner does not enforce equality.
"""

from __future__ import annotations

from app.verification.runner import Pillar


def pre_teardown_pillars(*, include_migration: bool = True) -> list[Pillar]:
    """Return the ordered list of pillars that run BEFORE teardown.

    These are the pillars that exercise the live tenant and its assets
    (api keys, luciel instances, domains, memory, audit log, scope
    assignments). They run in the throwaway tenant context produced by
    ``RunState`` and depend on the API surface being live at
    ``BASE_URL``.

    Imports are deferred to call-time so callers that only need the
    teardown-integrity pillar don't pull in the full dependency graph.

    Args
    ----
    include_migration:
        Whether to include Pillar 9 (migration integrity). The
        ``--skip-migration`` CLI flag passes ``False`` here; it's a
        DB-introspection pillar that is the slowest single pillar and
        sometimes deferred for fast iteration. CI gates and the prod
        verify run always pass ``True`` (the default).

    Returns
    -------
    list[Pillar]
        Ordered list of Pillar instances. Caller is responsible for
        registering them with a SuiteRunner in the order returned.
    """
    from app.verification.tests.pillar_01_onboarding import PILLAR as P1
    from app.verification.tests.pillar_02_scope_hierarchy import PILLAR as P2
    from app.verification.tests.pillar_03_ingestion import PILLAR as P3
    from app.verification.tests.pillar_04_chat_key_binding import PILLAR as P4
    from app.verification.tests.pillar_05_chat_resolution import PILLAR as P5
    from app.verification.tests.pillar_06_retention import PILLAR as P6
    from app.verification.tests.pillar_07_cascade import PILLAR as P7
    from app.verification.tests.pillar_08_scope_negatives import PILLAR as P8
    from app.verification.tests.pillar_09_migration_integrity import PILLAR as P9
    from app.verification.tests.pillar_11_async_memory import PILLAR as P11
    from app.verification.tests.pillar_12_identity_stability import PILLAR as P12
    from app.verification.tests.pillar_13_cross_tenant_identity import PILLAR as P13
    from app.verification.tests.pillar_14_departure_semantics import PILLAR as P14
    from app.verification.tests.pillar_15_consent_route_no_double_prefix import PILLAR as P15
    from app.verification.tests.pillar_16_memory_items_actor_user_id_not_null import PILLAR as P16
    from app.verification.tests.pillar_17_api_key_deactivate_audit import PILLAR as P17
    from app.verification.tests.pillar_18_tenant_cascade import PILLAR as P18
    from app.verification.tests.pillar_19_audit_log_api_mount import PILLAR as P19
    from app.verification.tests.pillar_20_onboarding_audit import PILLAR as P20
    from app.verification.tests.pillar_21_cross_tenant_scope_leak import PILLAR as P21
    from app.verification.tests.pillar_22_db_grants_audit_log_append_only import PILLAR as P22
    from app.verification.tests.pillar_23_audit_log_hash_chain import PILLAR as P23
    from app.verification.tests.pillar_24_luciel_instance_forensic_toggle import PILLAR as P24

    # Order matches the landed list at __main__.py prior to D, with P9 placed at
    # the end of the pre-teardown segment so a slow schema-introspection failure
    # does not block the live-API pillars (1-8, 11-24) from reporting first.
    # Step 29.x: P24 appended after P23 to close the C.5 toggle-route
    # verify-debt thesis (G1 authz, G2 audit emission, G3 no-op idempotency).
    pillars: list[Pillar] = [
        P1, P2, P3, P4, P5, P6, P7, P8,
        P11, P12, P13, P14, P15, P16, P17, P18, P19, P20, P21, P22, P23, P24,
    ]
    if include_migration:
        pillars.append(P9)
    return pillars


def teardown_integrity_pillar() -> Pillar:
    """Return the post-teardown pillar (P10).

    P10 verifies that the throwaway tenant left zero residue across all
    15 step26-tracked tables. It runs AFTER teardown -- registering it
    in the pre-teardown list would always FAIL because the rows are
    still live. Kept separate from ``pre_teardown_pillars`` for that
    reason.
    """
    from app.verification.tests.pillar_10_teardown_integrity import PILLAR as P10
    return P10


__all__ = ["pre_teardown_pillars", "teardown_integrity_pillar"]
