"""
Trace model.

Stores a structured record of what happened during each chat turn.
This is Luciel's observability layer — every request produces a trace
so you can debug, audit, and improve the system over time.

Includes tenant/domain/agent config references so you can see which
child Luciel configuration was active for each request.
"""

from __future__ import annotations

from sqlalchemy import ARRAY, BigInteger, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Trace(Base, TimestampMixin):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- Request context ---
    trace_id: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    """Unique trace identifier for this request."""

    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    domain_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Agent ID for per-agent audit trail."""

    # --- Input/Output ---
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_reply: Mapped[str] = mapped_column(Text, nullable=False)

    # --- LLM details ---
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Memory ---
    memories_retrieved: Mapped[int] = mapped_column(default=0, nullable=False)
    memories_used: Mapped[list | None] = mapped_column(JSON, nullable=True)
    memories_extracted: Mapped[int] = mapped_column(default=0, nullable=False)

    # --- Tools ---
    tool_called: Mapped[bool] = mapped_column(default=False, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Policy ---
    escalated: Mapped[bool] = mapped_column(default=False, nullable=False)
    policy_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # --- Config references: ALL REMOVED (Arc 10.5) ---
    # Anchored to Vision v1 §3 (five configuration pillars on the
    # Instance row; no Domain or Agent layer) and Architecture v1
    # §3.2 (Instance subsystem). The legacy
    # TenantConfig -> DomainConfig -> AgentConfig chain was
    # eliminated before Arc 10; the underlying tables were DROPPED.
    # traces.{tenant,domain,agent}_config_id were dead pointers that
    # never received content in V2. The arc10_5_drop_dead_config_id
    # _columns migration drops all three; the Mapped attributes are
    # removed here. The free-text traces.domain_id / traces.agent_id
    # columns are KEPT (historical pre-V2 forensic content).

    # Arc 9.1 Phase A (2026-05-25): NOT NULL.
    # Pre-Arc 9.1 doctrine: NULL = legacy/unbound. That doctrine created
    # the P2 RLS bypass (luciel_instance_id IS NULL clause in policy).
    # All NULL rows were wiped in arc9_1_a_tenant_isolation_seal.
    # Arc 5 Revision C / Arc 9.2 PR #99 — FK target is `instances.id`
    # (the `luciel_instances` table was dropped in Arc 5 Revision C).
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_traces_luciel_instance_id",
        ),
        nullable=False,
        index=True,
    )

    # Arc 11 Step 1 (arc11_a_knowledge_sources_schema) — knowledge_sources
    # IDs that contributed chunks to this turn. Populated by the
    # retriever in Arc 11 Step 5; until then stays empty. Queried by
    # the delete-confirm modal (Architecture §3.2.2) to show the
    # customer-question preview before a source is soft-deleted.
    # GIN-indexed in the migration for fast @> lookups.
    source_ids_used: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger),
        nullable=False,
        server_default="{}",
    )