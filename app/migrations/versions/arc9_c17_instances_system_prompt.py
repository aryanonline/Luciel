"""Arc 9 C17 -- add instances.system_prompt_additions for per-Instance persona.

Background (2026-05-25, Phase A.5 Free signup demo, post-C16):

  The chat service (``app/services/chat_service.py:230``) reads
  ``instance.system_prompt_additions`` to compose the per-turn system
  prompt under the four-layer doctrine (Luciel Core -> tenant
  additions -> domain additions -> instance additions). But the
  ``instances`` table never had this column -- it was carried over
  from the legacy ``agents`` table at the Arc 5 Admin->Instance
  collapse, never re-created on the new table.

  Result: every Instance ran on default Luciel Core + tenant/domain
  prompt fragments only. Customer-facing form labelled
  "Persona & instructions" was silently cosmetic; submitted values
  were dropped at the Pydantic boundary because ``InstanceCreate``
  doesn't accept the field either.

  This breaks the domain-agnostic vision: Sarah's real-estate
  concierge persona, an HVAC tech's lead-qualifier persona, and an
  e-commerce returns persona are indistinguishable because none of
  them persist their persona text. Every Instance behaves identically.

  Surfaced by partner Aryan at Phase A.5 step 4 when reviewing the
  "Create Luciel" UX before clicking submit.

Schema change
-------------

ADD COLUMN ``instances.system_prompt_additions TEXT NULL``.

* Nullable: yes -- existing rows have no persona text and don't need
  backfill. NULL is interpreted as "no instance-level additions" by
  chat_service._compose_system_prompt_additions(), which already
  guards on falsy.
* No default: explicit per-Instance persona is the contract. A
  default would lie about the operator's intent.
* No server_default: persona is operator-supplied at create time;
  the DB has no opinion.
* TEXT (unbounded): system prompts can be lengthy -- multi-paragraph
  instructions, few-shot examples, tool guidance. We bound at the
  Pydantic layer (8000 chars) for cost / context-window discipline.

Rollback safety
---------------

``alembic downgrade -1`` drops the column. Any non-NULL persona
text in operator-created rows is destroyed -- this is expected for a
schema rollback. Operators who downgrade should export persona text
first; we will surface this in the corrigendum doc and the runbook.

Wall-3 RLS posture
------------------

This column does NOT widen the RLS attack surface. ``instances`` is
already tenant-fenced by ``admin_isolation`` (PERMISSIVE) and Wall-3
fence; the new column inherits the row's existing visibility.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "arc9_c17_instances_system_prompt"
down_revision = "arc9_c14_add_tenant_permissive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column(
            "system_prompt_additions",
            sa.Text(),
            nullable=True,
            comment=(
                "Per-Instance system prompt fragment appended to Luciel "
                "Core. See chat_service._compose_system_prompt_additions. "
                "NULL = no instance-level additions; default behaviour."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("instances", "system_prompt_additions")
