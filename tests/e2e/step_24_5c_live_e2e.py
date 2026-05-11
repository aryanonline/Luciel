"""
Step 24.5c — Live end-to-end harness against the v1 success criterion
in docs/CANONICAL_RECAP.md §12 (row "24.5c") and DRIFTS.md
D-step-24-5c-impl-backlog-2026-05-11.

This is NOT a unit test. It is a live exercise of the SHIPPED code
paths — real IdentityResolver, real SessionService, real
SessionRepository, real CrossSessionRetriever, real ORM models —
against the recap's v1 success criterion for Step 24.5c. The point
is to demonstrate the cross-channel-identity primitives compose
end-to-end against the production code, not against inline fakes.

The v1 success criterion (verbatim from CANONICAL_RECAP §12 24.5c row
and the DRIFTS impl-backlog token):

    Two distinct channels (the embeddable chat widget and the
    programmatic API) exchange messages on two sessions joined by
    ONE conversation_id and ONE identity_claims row, with the
    CrossSessionRetriever surfacing the sibling session's recent
    turns inside the foundation-model context of the second turn.

Each numbered claim below maps to a piece of that criterion. The
script asserts every claim and prints a row per claim. Exit code 0 =
all claims satisfied. Non-zero = at least one claim violated.

POSTGRES REQUIRED. Unlike Step 30c's e2e (which can run against
sqlite:///:memory:), Step 24.5c's schema uses a native Postgres
enum (identity_claim_type) and uuid columns on conversations /
identity_claims that sqlite cannot represent without lossy casts.
Run against a real Postgres dev DB:

    DATABASE_URL="postgresql+psycopg2://USER:PASS@HOST:PORT/DBNAME" \
        python tests/e2e/step_24_5c_live_e2e.py

The script creates a fresh tenant fixture each run (timestamp-suffixed
tenant_id) and does NOT clean up — re-runs are idempotent because the
fixture id is unique. The CrossSessionRetriever's three-layer scope
filter is exercised both via the SQL filter and via the
defense-in-depth post-query loop (an unrelated noise conversation in
the same tenant is created to prove the scope filter excludes it).
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

# The resolver / models import the engine at import time. Insist on
# Postgres here — sqlite cannot honour the native enum + uuid columns
# Step 24.5c's migration installs.
_DB_URL = os.environ.get("DATABASE_URL", "")
if not _DB_URL.startswith("postgresql"):
    print(
        "ERROR: Step 24.5c live e2e requires Postgres. "
        "Set DATABASE_URL=postgresql+psycopg2://... and re-run."
    )
    print(f"  Current DATABASE_URL={_DB_URL!r}")
    sys.exit(2)

from sqlalchemy.orm import Session as SqlSession  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.identity.resolver import IdentityResolver  # noqa: E402
from app.memory.cross_session_retriever import (  # noqa: E402
    CrossSessionRetriever,
)
from app.models.conversation import Conversation  # noqa: E402
from app.models.identity_claim import (  # noqa: E402
    ClaimType,
    IdentityClaim,
)
from app.models.message import MessageModel  # noqa: E402
from app.models.session import SessionModel  # noqa: E402
from app.models.tenant import TenantConfig  # noqa: E402
from app.repositories.session_repository import SessionRepository  # noqa: E402
from app.services.session_service import SessionService  # noqa: E402


# ---------------------------------------------------------------------------
# Test harness scaffolding (same shape as tests/e2e/step_30c_live_e2e.py)
# ---------------------------------------------------------------------------


class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


results: list[ScenarioResult] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append(ScenarioResult(name, passed, detail))
    flag = "PASS" if passed else "FAIL"
    print(f"  [{flag}] {name}")
    if detail:
        print(f"         {detail}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def must_be(actual: Any, expected: Any, label: str) -> bool:
    ok = actual == expected
    if not ok:
        print(f"         {label}: expected={expected!r} actual={actual!r}")
    return ok


# ---------------------------------------------------------------------------
# Fixture setup — fresh tenant per run so re-runs don't collide on the
# unique constraint uq_identity_claims_type_value_scope.
# ---------------------------------------------------------------------------

RUN_STAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
TENANT_ID = f"step24_5c_e2e_{RUN_STAMP}"
DOMAIN_ID = "real_estate"
NOISE_DOMAIN_ID = "insurance"  # for scope-filter proof
CLAIM_EMAIL = f"prospect-{RUN_STAMP}@example.test"


def install_tenant_fixture(db: SqlSession) -> None:
    """Create the tenant row so the conversations + identity_claims
    FKs resolve. allowed_domains lists both DOMAIN_ID and
    NOISE_DOMAIN_ID so the noise-conversation in CLAIM 5 can also
    bind to this tenant.
    """
    db.add(
        TenantConfig(
            tenant_id=TENANT_ID,
            display_name=f"Step 24.5c E2E {RUN_STAMP}",
            description="Ephemeral fixture for live e2e — safe to leave behind.",
            allowed_domains=[DOMAIN_ID, NOISE_DOMAIN_ID],
            active=True,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------

print(f"Step 24.5c live e2e — tenant_id={TENANT_ID}")
print(f"  DATABASE_URL host present (redacted)")

db: SqlSession = SessionLocal()

try:
    install_tenant_fixture(db)

    # =====================================================================
    # CLAIM 1 — First channel (widget) creates a session via the §3.3
    # step 4 hook. The hook MUST resolve a brand-new identity (mint
    # User + IdentityClaim + Conversation in one txn) and bind the
    # new session to the resolution.
    # =====================================================================
    header("CLAIM 1 — Widget adapter asserts EMAIL claim, gets fresh identity")

    repo = SessionRepository(db=db)
    svc = SessionService(repository=repo)

    bundle1 = svc.create_session_with_identity(
        tenant_id=TENANT_ID,
        domain_id=DOMAIN_ID,
        agent_id=None,
        channel="web",  # widget channel
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL,
        issuing_adapter="widget",
    )
    db.commit()

    session1 = bundle1.session

    claim_1a = bundle1.is_new_user is True
    claim_1b = bundle1.is_new_conversation is True
    claim_1c = isinstance(bundle1.conversation_id, uuid.UUID)
    claim_1d = isinstance(bundle1.identity_claim_id, uuid.UUID)
    claim_1e = session1.conversation_id == bundle1.conversation_id
    claim_1f = session1.channel == "web"
    claim_1g = session1.tenant_id == TENANT_ID
    claim_1h = session1.domain_id == DOMAIN_ID

    record("widget call mints a new user (is_new_user=True)", claim_1a)
    record("widget call mints a new conversation (is_new_conversation=True)", claim_1b)
    record("conversation_id is a real uuid.UUID", claim_1c)
    record("identity_claim_id is a real uuid.UUID", claim_1d)
    record("new session.conversation_id == resolved conversation_id", claim_1e)
    record("new session.channel == 'web' (widget channel)", claim_1f)
    record("new session.tenant_id is the asserted scope", claim_1g)
    record("new session.domain_id is the asserted scope", claim_1h)

    # =====================================================================
    # CLAIM 2 — A turn lands on session 1 (widget). The cross-session
    # retriever, queried with the new conversation_id, sees this turn
    # only when its own session is NOT excluded — and sees nothing
    # when session 1 IS excluded (no siblings yet).
    # =====================================================================
    header("CLAIM 2 — Widget turn lands; retriever sees it / no siblings yet")

    db.add(
        MessageModel(
            session_id=session1.id,
            role="user",
            content="hi, can you tell me about 123 Maple Street?",
        )
    )
    db.add(
        MessageModel(
            session_id=session1.id,
            role="assistant",
            content="I'd be happy to. It's a 3-bed listed at $850k. What can I help with?",
        )
    )
    db.commit()

    retriever = CrossSessionRetriever(db=db)

    # Including session 1 → both messages visible.
    inc = retriever.retrieve(
        conversation_id=bundle1.conversation_id,
        tenant_id=TENANT_ID,
        domain_id=DOMAIN_ID,
        limit=20,
    )
    # Excluding session 1 → empty (no sibling sessions yet).
    exc = retriever.retrieve(
        conversation_id=bundle1.conversation_id,
        tenant_id=TENANT_ID,
        domain_id=DOMAIN_ID,
        limit=20,
        exclude_session_id=session1.id,
    )

    claim_2a = len(inc) == 2
    claim_2b = {p.role for p in inc} == {"user", "assistant"}
    claim_2c = all(p.source_session_id == session1.id for p in inc)
    claim_2d = all(p.source_channel == "web" for p in inc)
    claim_2e = len(exc) == 0  # no siblings yet

    record("retriever (no exclude) sees both widget turns", claim_2a)
    record("retriever surfaces both user + assistant roles", claim_2b)
    record("every passage's source_session_id == widget session", claim_2c)
    record("every passage's source_channel == 'web'", claim_2d)
    record("retriever (exclude widget session) → empty (no siblings yet)", claim_2e)

    # =====================================================================
    # CLAIM 3 — Second channel (programmatic_api) calls the §3.3 step
    # 4 hook with the SAME EMAIL claim. The resolver MUST take the
    # existing-claim path: same user_id, same conversation_id, same
    # identity_claim_id, but a brand-new session row on a different
    # channel.
    # =====================================================================
    header("CLAIM 3 — Programmatic API asserts same EMAIL, joins same identity")

    bundle2 = svc.create_session_with_identity(
        tenant_id=TENANT_ID,
        domain_id=DOMAIN_ID,
        agent_id=None,
        channel="programmatic_api",
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL,
        issuing_adapter="programmatic_api",
    )
    db.commit()

    session2 = bundle2.session

    claim_3a = bundle2.is_new_user is False
    claim_3b = bundle2.is_new_conversation is False
    claim_3c = bundle2.user_id == bundle1.user_id
    claim_3d = bundle2.conversation_id == bundle1.conversation_id
    claim_3e = bundle2.identity_claim_id == bundle1.identity_claim_id
    claim_3f = session2.id != session1.id
    claim_3g = session2.channel == "programmatic_api"
    claim_3h = session2.conversation_id == bundle1.conversation_id

    record("second call recognises existing user (is_new_user=False)", claim_3a)
    record("second call recognises existing conversation (is_new_conversation=False)", claim_3b)
    record("user_id is stable across the two channels", claim_3c)
    record("conversation_id is stable across the two channels", claim_3d)
    record("identity_claim_id is stable (one row, not two)", claim_3e)
    record("the two sessions are distinct rows (audit boundary preserved)", claim_3f)
    record("second session.channel == 'programmatic_api'", claim_3g)
    record("second session.conversation_id == first session.conversation_id", claim_3h)

    # =====================================================================
    # CLAIM 4 — Database-state truthing: ONE identity_claims row,
    # ONE conversations row, TWO sessions rows, all joined by the
    # same conversation_id. This is the "one claim, one conversation"
    # half of the success criterion enforced at the schema layer.
    # =====================================================================
    header("CLAIM 4 — Database state: one claim, one conversation, two sessions")

    claim_rows = (
        db.query(IdentityClaim)
        .filter(
            IdentityClaim.tenant_id == TENANT_ID,
            IdentityClaim.domain_id == DOMAIN_ID,
        )
        .all()
    )
    conv_rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == TENANT_ID,
            Conversation.domain_id == DOMAIN_ID,
        )
        .all()
    )
    session_rows = (
        db.query(SessionModel)
        .filter(
            SessionModel.tenant_id == TENANT_ID,
            SessionModel.domain_id == DOMAIN_ID,
        )
        .all()
    )

    claim_4a = len(claim_rows) == 1
    claim_4b = len(conv_rows) == 1
    claim_4c = len(session_rows) == 2
    claim_4d = {s.channel for s in session_rows} == {"web", "programmatic_api"}
    claim_4e = (
        len({s.conversation_id for s in session_rows}) == 1
        and session_rows[0].conversation_id is not None
    )
    claim_4f = (
        claim_rows[0].claim_type == ClaimType.EMAIL
        and claim_rows[0].claim_value == CLAIM_EMAIL.lower()
    )

    record("exactly ONE identity_claims row exists in scope", claim_4a)
    record("exactly ONE conversations row exists in scope", claim_4b)
    record("exactly TWO sessions rows exist in scope", claim_4c)
    record("the two sessions cover both channels (web + programmatic_api)", claim_4d)
    record("both sessions share the same non-null conversation_id", claim_4e)
    record("the identity_claims row is EMAIL + case-folded value", claim_4f)

    # =====================================================================
    # CLAIM 5 — The headline v1 criterion: the second session
    # (programmatic_api), at its first turn, asks the retriever for
    # context — and gets the WIDGET turns. This is the "the Luciel
    # picks up where the conversation left off" experience.
    #
    # Also exercises:
    #   - exclude_session_id (don't surface the api session's own
    #     future turns)
    #   - newest-first ordering
    #   - scope filter (noise conversation under a different
    #     domain_id should be invisible)
    # =====================================================================
    header("CLAIM 5 — Programmatic API picks up the widget conversation")

    # Plant a noise conversation in a different domain (same tenant)
    # so the scope filter has something concrete to exclude.
    noise_bundle = svc.create_session_with_identity(
        tenant_id=TENANT_ID,
        domain_id=NOISE_DOMAIN_ID,
        agent_id=None,
        channel="web",
        claim_type=ClaimType.EMAIL,
        claim_value=f"noise-{RUN_STAMP}@example.test",
        issuing_adapter="widget",
    )
    db.add(
        MessageModel(
            session_id=noise_bundle.session.id,
            role="user",
            content="NOISE — this turn lives under a different domain_id "
            "and MUST NOT surface in the programmatic_api's retriever call.",
        )
    )
    db.commit()

    # Now the programmatic_api turn calls the retriever asking for
    # the sibling-session context, excluding its own session.
    sibling_passages = retriever.retrieve(
        conversation_id=bundle2.conversation_id,
        tenant_id=TENANT_ID,
        domain_id=DOMAIN_ID,
        limit=20,
        exclude_session_id=session2.id,
    )

    claim_5a = len(sibling_passages) == 2
    claim_5b = all(p.source_session_id == session1.id for p in sibling_passages)
    claim_5c = all(p.source_channel == "web" for p in sibling_passages)
    # Newest first: assistant message was inserted after user message.
    claim_5d = (
        len(sibling_passages) >= 2
        and sibling_passages[0].timestamp >= sibling_passages[1].timestamp
    )
    claim_5e = not any(
        "NOISE" in p.content for p in sibling_passages
    )
    # Content survives the trip end-to-end.
    contents = {p.content for p in sibling_passages}
    claim_5f = any("123 Maple Street" in c for c in contents)

    record("retriever surfaces 2 sibling passages (both widget turns)", claim_5a)
    record("every sibling passage originates from the widget session", claim_5b)
    record("every sibling passage carries source_channel='web'", claim_5c)
    record("passages are ordered newest-first (recency rank, v1)", claim_5d)
    record(
        "noise conversation in a different domain_id is invisible "
        "(scope filter honoured)",
        claim_5e,
    )
    record("the original widget question text survives the round-trip", claim_5f)

    # =====================================================================
    # CLAIM 6 — Defense-in-depth: re-issuing the SAME claim from a
    # DIFFERENT scope mints a separate identity_claims row (per the
    # unique constraint's composite key), proving v1 cross-tenant
    # federation is correctly REJECTED at the schema layer (the §4.9
    # rejected-alternative bullet).
    # =====================================================================
    header("CLAIM 6 — Same email in a different domain → new claim row (scope-bounded)")

    bundle3 = svc.create_session_with_identity(
        tenant_id=TENANT_ID,
        domain_id=NOISE_DOMAIN_ID,
        agent_id=None,
        channel="web",
        claim_type=ClaimType.EMAIL,
        claim_value=CLAIM_EMAIL,  # SAME email, DIFFERENT domain
        issuing_adapter="widget",
    )
    db.commit()

    claim_6a = bundle3.is_new_user is True
    claim_6b = bundle3.user_id != bundle1.user_id
    claim_6c = bundle3.conversation_id != bundle1.conversation_id
    claim_6d = bundle3.identity_claim_id != bundle1.identity_claim_id

    record("same email under a different domain mints a NEW user", claim_6a)
    record("user_id is NOT shared across the domain boundary", claim_6b)
    record("conversation_id is NOT shared across the domain boundary", claim_6c)
    record("identity_claim_id is NOT shared across the domain boundary", claim_6d)

    # =====================================================================
    # SUMMARY
    # =====================================================================
    header("SUMMARY")

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    print(f"  Total claims:     {total}")
    print(f"  Passed:           {passed}")
    print(f"  Failed:           {failed}")
    print()

    if failed:
        print("  FAILED CLAIMS:")
        for r in results:
            if not r.passed:
                print(f"    - {r.name}")
        print()
        print("  Step 24.5c v1 success criterion is NOT fully satisfied.")
        print("  Do NOT cut the step-24-5c-cross-channel-identity-complete tag.")
        sys.exit(1)
    else:
        print("  All Step 24.5c v1 success-criterion claims are satisfied")
        print("  against the live shipped code (real Postgres, real ORM,")
        print("  real IdentityResolver + SessionService + CrossSessionRetriever).")
        print()
        print("  Safe to cut tag: step-24-5c-cross-channel-identity-complete")
        sys.exit(0)

finally:
    db.close()
