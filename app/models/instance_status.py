"""InstanceStatus — canonical lifecycle states for an Instance.

Arc 11 Closeout PR-A. Mirrors the PostgreSQL ``instance_status`` enum
created by Alembic revision ``arc11_closeout_a_instance_lifecycle``.

Three lifecycle states (locked by founder):

* ``active``  — chat widget serves, knowledge ingest open, normal ops.
* ``paused``  — operational quiet (Customer Journey §4.5 Phase 8 "Pause
                my Luciel"). Widget renders empty ``<div>``; data
                retained; reactivatable instantly via /resume.
* ``deleted`` — destructive intent (Customer Journey §4.5 Phase 8
                "Delete this instance"). ``soft_deleted_at`` stamped;
                knowledge + conversations enter a 30-day grace window
                (Architecture §3.6.1); /restore reactivates within the
                window and re-mints embed keys (Vision §6.4).

The legacy ``active`` boolean column is retained through Arc 11; this
enum is the new source of truth and the column is the deprecated mirror
(slated for drop in Arc 12).
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
    DELETED = "deleted"


INSTANCE_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in InstanceStatus)
