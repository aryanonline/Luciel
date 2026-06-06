"""Downgrade archive service — V2 Arc 6 Commit 8.5b.

The single place that runs **overflow archive at a downgrade boundary**.
Invoked by the webhook ``_on_subscription_deleted`` V2 branch (when the
sub's ``pending_downgrade_target`` is set), AFTER ``downgrade_admin_tier``
has flipped the Admin row to the new tier.

Doctrine (CANONICAL_RECAP §17 Commit 8.5b lock):

* **Policy = LRU soft-archive with rehydrate window.** When the admin
  holds more of any cap'd resource than the destination tier allows,
  the *least-recently-updated* overflow rows are archived. The stamp
  ``pending_downgrade_archived_at`` is what later distinguishes
  "archived because of downgrade" from other soft-delete states and
  lets a re-upgrade within the ``audit_retention`` window rehydrate.

* **Four resource axes are capped per tier** (§14 entitlement matrix):
    - ``instance_count_cap``           -> ``instances`` table
    - ``embed_key_count_cap``          -> ``api_keys`` (key_kind='embed')
    - ``widget_custom_domain_cname_cap`` -> ``admin_widget_domains``
    - ``seat_cap``                     -> ``scope_assignments``
  The first three use the new ``pending_downgrade_archived_at`` stamp;
  the fourth reuses the existing Pattern E end-assignment columns
  (``ended_at`` + ``ended_reason='DOWNGRADE_OVERFLOW_ARCHIVE'`` +
  ``active=false``) per the schema decision in
  ``arc6_c_pending_downgrade_columns``.

* **Owner seat is exempt.** Cap-checks against ``seat_cap`` honor the
  owner-scope-assignment as an always-kept row even if it would
  otherwise be the LRU loser. A buyer can never archive themselves
  out of their own Admin.

* **Cap = None means unlimited** (Enterprise). Calling
  ``archive_overflow_for_admin`` with the Admin already at Enterprise
  is a noop on every axis. Pro/Free have finite caps; the service
  loops only when ``cap is not None``.

* **Atomic per-axis, autocommit at the end.** The four per-axis
  archives run within one SQLAlchemy transaction. Either all four
  commit or none commit; partial archives are not a state we ever
  want to leave behind.

* **Reusable for preview.** ``preview_overflow_for_admin`` runs the
  same LRU sort + count math but mutates nothing. Used by the
  frontend soft-warn confirm modal ("Pro → Free will archive N
  instances, M embed keys, …, effective on <date>"). Returning a
  rich preview shape lets the UI render the modal without the
  archive service needing a parallel implementation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.orm import Session

from app.models.admin_widget_domain import AdminWidgetDomain
from app.models.api_key import ApiKey
from app.models.instance import Instance
from app.models.knowledge import KnowledgeChunk  # Arc 10: AXIS_KNOWLEDGE
# Unit 1 excision: scope_assignment model deleted; AXIS_SEATS removed below.
from app.policy.entitlements import (
    TIER_FREE,
    TIER_PRO,
    TIER_ENTITLEMENTS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result shape — used by both preview and apply paths.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AxisOverflow:
    """Per-axis overflow tally used by the preview/apply contract.

    * ``axis``        — one of {'instances', 'embed_keys', 'cnames', 'seats'}.
                        Stable string keys for the JSON response shape.
    * ``cap``         — destination-tier cap on this axis. ``None`` means
                        unlimited (Enterprise); the service never lists
                        this axis as overflow when cap is None.
    * ``current``     — count of currently-active rows on this axis for
                        the admin (the rows the cap is applied against).
    * ``overflow``    — ``max(current - cap, 0)``. The number of rows to
                        archive (preview) or that were archived (apply).
    * ``archived_ids``— PKs of the rows the LRU sort selected for archive.
                        Populated on apply; on preview, populated with the
                        rows that WOULD be archived so the UI can render
                        them (\"these 3 instances will be archived: …\").
    """

    axis: str
    cap: int | None
    current: int
    overflow: int
    archived_ids: list[int | str] = field(default_factory=list)


@dataclass(frozen=True)
class OverflowSummary:
    """Aggregate result of a preview or apply call.

    The frontend modal renders this verbatim; the webhook logs it.
    """

    admin_id: str
    target_tier: str
    archived_at: datetime | None  # None on preview, set on apply
    axes: dict[str, AxisOverflow]

    @property
    def total_overflow(self) -> int:
        """Total rows across all axes selected for archive."""
        return sum(a.overflow for a in self.axes.values())

    @property
    def any_overflow(self) -> bool:
        """True iff at least one axis has rows to archive.

        Used by the route layer's preview endpoint to decide whether the
        soft-warn modal should show overflow language at all. A Free
        admin downgrading to Free (impossible at the route layer, but
        defensive) or a Pro admin with very few rows downgrading to Free
        may have zero overflow on all axes.
        """
        return self.total_overflow > 0


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

# Axis keys are public — they appear in the JSON response shape consumed
# by Account.tsx's confirm modal. Keep stable; do not rename without
# updating the frontend in the same commit.
AXIS_INSTANCES = "instances"
AXIS_EMBED_KEYS = "embed_keys"
AXIS_CNAMES = "cnames"
# AXIS_SEATS removed (Unit 1 excision) — single-owner model has no multi-seat table.
# Arc 10: knowledge axis. Unlike the count-of-rows axes above,
# AXIS_KNOWLEDGE is a sum-of-bytes axis whose unit is the INTEGER
# source_id FK (a group of chunks sharing the same source FK).
# LRU selection picks oldest sources by their newest chunk's
# updated_at, and archives whole sources until total bytes <= cap.
# All chunks sharing a source FK archive together.
AXIS_KNOWLEDGE = "knowledge"
ALL_AXES: tuple[str, ...] = (
    AXIS_INSTANCES, AXIS_EMBED_KEYS, AXIS_CNAMES,
    AXIS_KNOWLEDGE,
)


class DowngradeArchiveService:
    """Compute + apply overflow archive at a downgrade boundary.

    Lifetime: one instance per webhook call OR per preview request.
    The bound ``Session`` is request-scoped.

    No external dependencies beyond the ORM. Audit emission for the
    archive rows lives in the calling webhook context (the audit row
    needs Stripe event metadata that this service doesn't see); this
    service logs at INFO with structured fields for the audit chain.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # -----------------------------------------------------------------
    # Public — preview (no writes)
    # -----------------------------------------------------------------

    def preview_overflow_for_admin(
        self,
        *,
        admin_id: str,
        target_tier: str,
    ) -> OverflowSummary:
        """Compute what would be archived if ``admin_id`` downgraded to
        ``target_tier`` right now. **Mutates nothing.**

        Used by:
          * ``POST /api/v1/billing/downgrade/preview`` (Account.tsx modal)
          * ``archive_overflow_for_admin`` itself (apply path reuses the
            sort to avoid drift between preview and apply)

        Returns an OverflowSummary with ``archived_at=None`` and each
        axis populated with the PKs the LRU sort would select.
        """
        _validate_target_tier(target_tier)
        axes = {axis: self._compute_axis(admin_id, target_tier, axis)
                for axis in ALL_AXES}
        return OverflowSummary(
            admin_id=admin_id,
            target_tier=target_tier,
            archived_at=None,
            axes=axes,
        )

    # -----------------------------------------------------------------
    # Public — apply (mutates within a single transaction)
    # -----------------------------------------------------------------

    def archive_overflow_for_admin(
        self,
        *,
        admin_id: str,
        target_tier: str,
        autocommit: bool = True,
    ) -> OverflowSummary:
        """Run the overflow archive for ``admin_id`` against
        ``target_tier``'s caps. **Mutates rows in 4 tables.**

        Steps:
          1. Compute the same LRU sort the preview path would produce.
          2. For each axis with overflow > 0, stamp the selected rows:
              * Instances: ``active=False`` + ``pending_downgrade_archived_at=NOW``
              * Embed keys: same (filtered to key_kind='embed')
              * CNAMEs: ``pending_downgrade_archived_at=NOW`` (table
                has no separate ``active`` column; stamp alone is the
                soft-delete marker on this table)
              * Seats: end_assignment with EndReason.DOWNGRADE_OVERFLOW_ARCHIVE
          3. Commit (if autocommit; the webhook caller may compose with
             tier_provisioning under one txn).

        Idempotency:
          The LRU query already excludes rows that carry a non-NULL
          ``pending_downgrade_archived_at`` (they're not active). A
          replay of the same downgrade boundary archives nothing new
          and reports zero overflow on the second call.

        Returns:
          OverflowSummary with ``archived_at`` set to the apply-time
          UTC timestamp and ``archived_ids`` populated per axis.
        """
        _validate_target_tier(target_tier)

        archived_at = datetime.now(timezone.utc)
        axes: dict[str, AxisOverflow] = {}

        for axis in ALL_AXES:
            tally = self._compute_axis(admin_id, target_tier, axis)
            if tally.overflow > 0:
                self._apply_axis(
                    admin_id=admin_id,
                    axis=axis,
                    archive_ids=tally.archived_ids,
                    archived_at=archived_at,
                )
                logger.info(
                    "downgrade_archive: admin=%s axis=%s archived=%d cap=%s "
                    "current=%d target_tier=%s",
                    admin_id, axis, tally.overflow, tally.cap,
                    tally.current, target_tier,
                )
            axes[axis] = tally

        # --------------------------------------------------------------
        # §3.6.7 DORMANT: on any downgrade (Pro→Free), set all live
        # action-tool connections to status='dormant'.  Secrets are
        # RETAINED (credential_ref is not touched); prior status is
        # stored in status_detail for restore on re-upgrade.  This is
        # separate from the overflow-archive axes above (those remove
        # row-level resources; dormant just changes the connection status).
        # --------------------------------------------------------------
        from app.repositories.instance_connection_repository import (
            InstanceConnectionRepository,
        )
        conn_repo = InstanceConnectionRepository(self.db)
        dormant_rows = conn_repo.set_dormant_for_admin(
            admin_id=admin_id,
            autocommit=False,
        )
        if dormant_rows:
            logger.info(
                "downgrade_archive: admin=%s dormant_connections=%d "
                "target_tier=%s (secrets retained, restore on re-upgrade)",
                admin_id, len(dormant_rows), target_tier,
            )

        if autocommit:
            self.db.commit()

        return OverflowSummary(
            admin_id=admin_id,
            target_tier=target_tier,
            archived_at=archived_at,
            axes=axes,
        )

    # -----------------------------------------------------------------
    # Per-axis computation — single dispatch by axis name.
    # -----------------------------------------------------------------

    def _compute_axis(
        self,
        admin_id: str,
        target_tier: str,
        axis: str,
    ) -> AxisOverflow:
        """LRU sort + cap check for a single axis. No writes."""
        cap = _cap_for_axis(target_tier, axis)

        # Unlimited (Enterprise destination, or any axis where the
        # destination tier has no cap): always zero overflow.
        if cap is None:
            return AxisOverflow(axis=axis, cap=None, current=0, overflow=0)

        # Arc 10: AXIS_KNOWLEDGE is a sum-of-bytes axis, not
        # count-of-rows. Special-cased here so the existing count
        # path stays untouched for the four original axes.
        if axis == AXIS_KNOWLEDGE:
            return self._compute_knowledge_axis(admin_id, cap)

        active_rows = self._active_rows_for_axis(admin_id, axis)
        current = len(active_rows)
        overflow = max(current - cap, 0)

        if overflow == 0:
            return AxisOverflow(axis=axis, cap=cap, current=current, overflow=0)

        # LRU loser selection.
        candidates = active_rows
        ids = _lru_select(candidates, overflow)
        return AxisOverflow(
            axis=axis, cap=cap, current=current,
            overflow=overflow, archived_ids=ids,
        )

    # -----------------------------------------------------------------
    # AXIS_KNOWLEDGE -- sum-of-bytes overflow with source-id grouping.
    # -----------------------------------------------------------------

    def _compute_knowledge_axis(
        self,
        admin_id: str,
        cap_bytes: int,
    ) -> AxisOverflow:
        """Compute the knowledge axis overflow for one admin.

        Unit-of-archive is a *source group*. Post-Cleanup-B every
        chunk carries a non-NULL INTEGER ``source_id`` FK to
        ``knowledge_sources.id``, so the bucket key is the FK
        directly (the pre-Cleanup-B prefixed key that disambiguated
        FK-vs-legacy-string is gone with the legacy columns).

        Per-source size approximated by ``SUM(LENGTH(content))`` over
        active chunks of that source.

        LRU sort: oldest source first, where "age" is the source's
        most-recent chunk's ``updated_at``. A source that has been
        recently re-ingested or edited counts as recently-used.

        Selection rule: take sources in LRU order, accumulate their
        bytes, stop when total active bytes (after archiving the
        selected sources) is <= cap.

        Returns ``AxisOverflow`` whose ``archived_ids`` field is a
        list of integer source PKs. ``_apply_axis`` consumes them.
        """
        from sqlalchemy import func, select as sa_select

        per_source_stmt = (
            sa_select(
                KnowledgeChunk.source_id.label("bucket"),
                func.sum(func.length(KnowledgeChunk.content)).label("bytes"),
                func.max(KnowledgeChunk.updated_at).label("recency"),
            )
            .where(
                KnowledgeChunk.admin_id == admin_id,
                KnowledgeChunk.superseded_at.is_(None),
                KnowledgeChunk.soft_deleted_at.is_(None),
                KnowledgeChunk.pending_downgrade_archived_at.is_(None),
            )
            .group_by(KnowledgeChunk.source_id)
        )
        rows = list(self.db.execute(per_source_stmt).all())

        # Compute current total bytes.
        current_bytes = sum(int(r.bytes or 0) for r in rows)
        if current_bytes <= cap_bytes:
            return AxisOverflow(
                axis=AXIS_KNOWLEDGE,
                cap=cap_bytes,
                current=current_bytes,
                overflow=0,
            )

        # LRU sort by recency ascending (oldest first), tiebreak by
        # bucket so two sources with identical recency archive in
        # deterministic order.
        rows.sort(key=lambda r: (r.recency or datetime.min, r.bucket))

        # Greedy: archive sources oldest-first until we are under cap.
        archived_source_ids: list[int] = []
        bytes_remaining = current_bytes
        for r in rows:
            if bytes_remaining <= cap_bytes:
                break
            archived_source_ids.append(int(r.bucket))
            bytes_remaining -= int(r.bytes or 0)

        return AxisOverflow(
            axis=AXIS_KNOWLEDGE,
            cap=cap_bytes,
            current=current_bytes,
            overflow=current_bytes - bytes_remaining,  # bytes archived
            archived_ids=archived_source_ids,
        )

    # -----------------------------------------------------------------
    # Per-axis active-row queries.
    # -----------------------------------------------------------------

    def _active_rows_for_axis(
        self, admin_id: str, axis: str,
    ) -> list:
        """Fetch the currently-active rows on a given axis for an admin.

        "Active" means: not previously archived (no stamp set), passes
        the table's own active flag where one exists.

        The LRU sort key is exposed on each row as ``updated_at`` (or
        ``started_at`` for scope_assignments — they have no updated_at
        on the lifecycle columns).
        """
        if axis == AXIS_INSTANCES:
            stmt = (
                select(Instance)
                .where(
                    Instance.admin_id == admin_id,
                    Instance.active.is_(True),
                    Instance.pending_downgrade_archived_at.is_(None),
                )
            )
            return list(self.db.scalars(stmt).all())

        if axis == AXIS_EMBED_KEYS:
            # admin_id on api_keys is the legacy column name for admin
            # binding — see D-arc5-tenant-id-column-physical-retention-2026-05-23.
            stmt = (
                select(ApiKey)
                .where(
                    ApiKey.admin_id == admin_id,
                    ApiKey.key_kind == "embed",
                    ApiKey.active.is_(True),
                    ApiKey.pending_downgrade_archived_at.is_(None),
                )
            )
            return list(self.db.scalars(stmt).all())

        if axis == AXIS_CNAMES:
            # admin_widget_domains has no `active` column; the stamp
            # alone discriminates archived from live.
            stmt = (
                select(AdminWidgetDomain)
                .where(
                    AdminWidgetDomain.admin_id == admin_id,
                    AdminWidgetDomain.pending_downgrade_archived_at.is_(None),
                )
            )
            return list(self.db.scalars(stmt).all())

        raise ValueError(f"DowngradeArchiveService: unknown axis {axis!r}")

    # -----------------------------------------------------------------
    # Per-axis apply.
    # -----------------------------------------------------------------

    def _apply_axis(
        self,
        *,
        admin_id: str,
        axis: str,
        archive_ids: Iterable,
        archived_at: datetime,
    ) -> None:
        """Stamp the archive on the selected rows for one axis.

        Each branch uses bulk UPDATE via the ORM so the change-set
        rides the caller's transaction. The seat branch uses the
        repository's ``end_assignment`` because that emits the right
        idempotency-guarded write and respects the ENUM type.
        """
        ids_list = list(archive_ids)
        if not ids_list:
            return

        if axis == AXIS_INSTANCES:
            # RESCAN CORE(serving-path) GAP-5 (§3.6.7) — set the
            # system-imposed pause on the column the live lifecycle gates
            # actually read. Pre-fix this branch wrote only the deprecated
            # ``active`` boolean; the gates key off ``instance_status``, so
            # an over-cap downgraded instance kept ``instance_status =
            # 'active'`` and kept serving + accruing budget. Stamping
            # INACTIVE makes check_instance_lifecycle (SMS/email) and the
            # widget gate drop it, and the orchestrator's lifecycle gate
            # short-circuits the /chat paths too.
            from app.models.instance_status import InstanceStatus

            for row in self.db.scalars(
                select(Instance).where(Instance.id.in_(ids_list))
            ).all():
                row.active = False
                row.instance_status = InstanceStatus.INACTIVE
                row.pending_downgrade_archived_at = archived_at
            return

        if axis == AXIS_EMBED_KEYS:
            for row in self.db.scalars(
                select(ApiKey).where(ApiKey.id.in_(ids_list))
            ).all():
                row.active = False
                row.pending_downgrade_archived_at = archived_at
            return

        if axis == AXIS_CNAMES:
            for row in self.db.scalars(
                select(AdminWidgetDomain).where(
                    AdminWidgetDomain.id.in_(ids_list)
                )
            ).all():
                row.pending_downgrade_archived_at = archived_at
            return

        if axis == AXIS_KNOWLEDGE:
            # Post-Cleanup-B: archive_ids are integer source PKs.
            # All chunks sharing the same FK archive together; the
            # parent source row gets a mirrored stamp so its own
            # lifecycle column stays consistent with its chunks.
            for source_pk in ids_list:
                self.db.execute(
                    sql_text(
                        """
                        UPDATE knowledge_chunks
                           SET pending_downgrade_archived_at = :ts
                         WHERE admin_id = :aid
                           AND source_id = :sid
                           AND superseded_at IS NULL
                           AND soft_deleted_at IS NULL
                           AND pending_downgrade_archived_at IS NULL
                        """
                    ),
                    {"ts": archived_at, "aid": admin_id, "sid": int(source_pk)},
                )
                self.db.execute(
                    sql_text(
                        """
                        UPDATE knowledge_sources
                           SET pending_downgrade_archived_at = :ts
                         WHERE id = :sid
                           AND admin_id = :aid
                           AND pending_downgrade_archived_at IS NULL
                        """
                    ),
                    {"ts": archived_at, "aid": admin_id, "sid": int(source_pk)},
                )
            return

        raise ValueError(f"DowngradeArchiveService: unknown axis {axis!r}")


# ---------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------

# Per-axis attribute on the TierEntitlement dataclass. Centralizing the
# axis -> attribute mapping here keeps the per-axis branches small.
_CAP_AXIS_TO_ATTR: dict[str, str] = {
    AXIS_INSTANCES:   "instance_count_cap",
    AXIS_EMBED_KEYS:  "embed_key_count_cap",
    AXIS_CNAMES:      "widget_custom_domain_cname_cap",
    # AXIS_SEATS removed (Unit 1 excision) — no seat_cap in entitlements.
    # Arc 10: knowledge cap is in BYTES, not in count-of-rows. The
    # _compute_axis path special-cases AXIS_KNOWLEDGE to compute
    # overflow as sum(bytes) - cap rather than count(rows) - cap.
    AXIS_KNOWLEDGE:   "knowledge_bytes_cap",
}


def _validate_target_tier(target_tier: str) -> None:
    """Pin the legal downgrade destinations.

    Enterprise is never a downgrade destination (it is the top tier);
    the schema-level CHECK on ``subscriptions.pending_downgrade_target``
    already rejects it, but we mirror the guard at the service boundary
    so a misrouted preview call fails fast with a clear error.
    """
    if target_tier not in (TIER_FREE, TIER_PRO):
        raise ValueError(
            f"DowngradeArchiveService: target_tier {target_tier!r} is not a "
            f"legal downgrade destination; expected one of "
            f"{{{TIER_FREE!r}, {TIER_PRO!r}}}."
        )


def _cap_for_axis(target_tier: str, axis: str) -> int | None:
    """Read the destination-tier cap on a given axis.

    Returns ``None`` for unlimited (Enterprise). The
    ``TIER_ENTITLEMENTS`` map is the canonical source.
    """
    attr = _CAP_AXIS_TO_ATTR.get(axis)
    if attr is None:
        raise ValueError(f"_cap_for_axis: unknown axis {axis!r}")
    return getattr(TIER_ENTITLEMENTS[target_tier], attr)


# _is_owner_seat removed (Unit 1 excision) — AXIS_SEATS deleted.


def _lru_select(rows: list, n: int) -> list:
    """Pick the ``n`` least-recently-updated row PKs.

    LRU sort key (in priority order):
      1. ``updated_at`` ascending — oldest update wins (Instance,
         ApiKey, AdminWidgetDomain).
      2. ``started_at`` ascending — fallback for rows lacking updated_at.
      3. PK ascending — deterministic tiebreak so two rows updated in
         the same microsecond archive in a stable order. Important
         for test reproducibility and replay.

    Returns the PK list (``.id`` accessor) in the same order they were
    selected.
    """
    if n <= 0 or not rows:
        return []

    def _sort_key(row):
        ts = getattr(row, "updated_at", None) or getattr(row, "started_at", None)
        # Coerce None to MIN datetime so rows lacking timestamps are
        # picked first — they're the most "stale" possible state.
        ts_sortable = ts or datetime.min.replace(tzinfo=timezone.utc)
        return (ts_sortable, row.id)

    sorted_rows = sorted(rows, key=_sort_key)
    return [r.id for r in sorted_rows[:n]]
