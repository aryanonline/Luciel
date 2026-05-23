"""AdminWidgetDomain ORM model — per-Admin widget embed-domain allowlist (Arc 6 A).

Mirrors the ``admin_widget_domains`` table created at Arc 6 Revision A
(``alembic/versions/arc6_a_admin_widget_domains.py``). The table is born
inside Arc 6 because the V2 SKU shape (Free / Pro / Enterprise) is where
the allowlist first carries product weight.

Schema anchors
--------------
* ``admin_id`` is ``VARCHAR(100)`` referencing ``admins(id)`` with
  ``ON DELETE CASCADE``. Domain rows have no audit value separate from
  the Admin they belong to (unlike ``scope_assignments`` or
  ``user_invites``, which use ``ON DELETE RESTRICT`` because they
  represent durable user-side state). Cascading is correct here:
  removing an Admin must remove their allowlist.
* ``domain`` is ``VARCHAR(253)`` per RFC 1035 max hostname length.
  Stored lowercased + apex-normalized by the app layer; the schema
  enforces the lowercased contract via
  ``ck_admin_widget_domains_domain_lowercase``.
* ``UNIQUE (admin_id, domain)`` — one Admin cannot register the same
  domain twice. No global uniqueness on ``domain`` alone (two Admins
  can independently allowlist the same hostname).
* ``ix_admin_widget_domains_admin_id`` supports the hot per-request
  check: ``SELECT 1 FROM admin_widget_domains WHERE admin_id = :a AND
  domain = :d``.

Tier limits (Pro caps the count, Enterprise is uncapped) are enforced
at the **app layer** against ``CANONICAL_RECAP.md §14``, not at the
schema layer. The schema is tier-agnostic.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminWidgetDomain(Base):
    __tablename__ = "admin_widget_domains"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey(
            "admins.id",
            name="fk_admin_widget_domains_admin_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "domain",
            name="uq_admin_widget_domains_admin_id_domain",
        ),
        CheckConstraint(
            "domain = lower(domain)",
            name="ck_admin_widget_domains_domain_lowercase",
        ),
        CheckConstraint(
            "length(domain) > 0 AND domain !~ '[[:space:]]'",
            name="ck_admin_widget_domains_domain_shape",
        ),
        Index("ix_admin_widget_domains_admin_id", "admin_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover — diagnostic
        return (
            f"<AdminWidgetDomain id={self.id!r} admin_id={self.admin_id!r} "
            f"domain={self.domain!r}>"
        )
