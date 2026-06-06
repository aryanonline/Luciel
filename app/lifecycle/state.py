"""InstanceStatus — canonical lifecycle states for an Instance.

RESCAN TIER-DE (lifecycle): Extend from 3 states to 5 states per
Architecture §3.6.1.

Five lifecycle states (§3.6.1 — locked):

* ``active``       — chat widget serves, knowledge ingest open, normal ops.
* ``paused``       — operational quiet (Customer Journey §4.5 Phase 8 "Pause
                     my Luciel"). Widget renders empty ``<div>``; data
                     retained; reactivatable instantly via /resume.
* ``deactivating`` — destructive intent has been signalled (DELETE requested);
                     the system is transitioning into the grace window.
                     Automatic transition to ``grace_window`` after system
                     processing (e.g. revoke sibling grants, embed keys).
* ``grace_window`` — 30-day data-retention clock has started from
                     ``soft_deleted_at``. Knowledge + conversations retained;
                     owner may reactivate to ``active``. The retention worker
                     transitions to ``hard_deleted`` at day 30.
* ``hard_deleted`` — all customer data has been hard-purged. The instance
                     row itself is deleted; this state is only visible in the
                     audit log tombstone.

Deprecated alias
----------------
``deleted`` is the legacy 3-state enum member. Existing rows with
``instance_status = 'deleted'`` are semantically equivalent to
``grace_window`` (soft_deleted_at has been stamped; 30-day clock is
running). The worker scan now includes BOTH ``'deleted'`` and
``'grace_window'`` so legacy rows are not orphaned. New code must use
the 5-state vocabulary; ``DELETED`` is preserved in this enum as
``deleted`` for backward-compat of existing queries/tests but maps to
the ``grace_window`` semantics.

Transition table (§3.6.1):

  active         → paused          (owner or manager)
  paused         → active          (owner or manager)
  active|paused  → deactivating    (owner or manager — DELETE request)
  deactivating   → grace_window    (automatic — system after grant revocation)
  grace_window   → active          (owner only — /restore within 30 days)
  grace_window   → hard_deleted    (automatic — retention worker at day 30)

Role gating implemented in ``app/services/instance_service.py`` which
enforces the transition table via ``InstanceTransitionError``.
"""

from __future__ import annotations

import enum


class InstanceStatus(str, enum.Enum):
    """Lifecycle status for an Instance row.

    String-valued so SQLAlchemy and Pydantic round-trip cleanly with
    the PostgreSQL ``instance_status`` enum (which stores the lower-
    case member names verbatim).
    """

    ACTIVE = "active"
    PAUSED = "paused"
    # Deprecated alias for grace_window; retained so existing rows and
    # queries using the 3-state vocabulary stay valid.  New code must
    # use GRACE_WINDOW.  See module docstring for the mapping rationale.
    DELETED = "deleted"
    DEACTIVATING = "deactivating"
    GRACE_WINDOW = "grace_window"
    HARD_DELETED = "hard_deleted"
    # NOTE: the non-spec ``INACTIVE`` member was REMOVED in Unit 4
    # (lifecycle alignment). It existed only for a multi-instance
    # over-cap downgrade path that is unreachable in the single-Luciel
    # model (instance_count_cap = 1) and contradicted the ratified
    # 5-state machine (§3.6.1). The PG enum value is dropped by the
    # unit4_drop_instance_status_inactive migration.


# Convenience set: states that represent "instance is in some form of
# soft-deleted / grace-period state" and therefore eligible for the
# retention worker's hard-purge scan.
INSTANCE_GRACE_STATES: frozenset[str] = frozenset({"deleted", "grace_window"})

# All legal states (matches the PG enum after the RESCAN TIER-DE migration).
INSTANCE_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in InstanceStatus)
