"""
P13 A3 root-cause reproduction (D-pillar-13-a3-real-root-cause-2026-05-04).

Mimics Pillar 13's setup-turn flow EXACTLY (just for the legit T1 side):
  1. Onboard tenant T1 via /admin/tenants/onboard (returns admin_raw_key)
  2. Create platform User U
  3. Create Agent A1 in T1 with contact_email
  4. Direct DB write: a1.user_id = U.id  (mirrors Pillar 13 line 228)
  5. Mint chat key K1 bound to A1
  6. POST /api/v1/sessions  (with end-user pseudonym)
  7. POST /api/v1/consent/grant
  8. POST /api/v1/chat with Pillar-13-style legitimate setup message
  9. Sleep 30s for worker to process
 10. Dump audit log, messages, memory_items for the tenant

Run:
  python diag_p13_repro.py

Requires:
  - uvicorn running on 127.0.0.1:8000 with the P13_DIAG-instrumented build
  - Celery worker running
  - LUCIEL_PLATFORM_ADMIN_KEY env var set

Output is to stdout. Recommend redirecting:
  python diag_p13_repro.py 2>&1 | Tee-Object -FilePath diag_p13_output.txt
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 60.0


def h(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def call(method: str, path: str, key: str, c: httpx.Client, *,
         json=None, expect=(200, 201)):
    r = c.request(method, path, headers=h(key), json=json)
    allowed = (expect,) if isinstance(expect, int) else tuple(expect)
    if r.status_code not in allowed:
        raise SystemExit(
            f"FAIL {method} {path}\n"
            f"  expected {allowed} got {r.status_code}\n"
            f"  body={r.text[:400]}"
        )
    return r


def main() -> int:
    pa = os.environ.get("LUCIEL_PLATFORM_ADMIN_KEY")
    if not pa:
        print("ERR: LUCIEL_PLATFORM_ADMIN_KEY not set in this shell")
        return 2

    suffix = uuid.uuid4().hex[:8]
    tenant_id = f"diag-p13-t1-{suffix}"
    domain_id = "general"
    agent_a1_slug = f"diag-p13-a1-{uuid.uuid4().hex[:6]}"
    user_email = f"diag-p13-user-{suffix}@example.com"
    end_user_pseudonym = f"diag-p13-end-user-{uuid.uuid4().hex[:6]}"

    print(f"=== P13 A3 reproduction starting ===")
    print(f"  tenant_id    = {tenant_id}")
    print(f"  agent slug   = {agent_a1_slug}")
    print(f"  user_email   = {user_email}")
    print(f"  end-user pseudonym = {end_user_pseudonym}")
    print()

    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        # 1. Onboard tenant T1 (mirrors Pillar 13 line 140)
        r = call(
            "POST", "/api/v1/admin/tenants/onboard", pa, c,
            json={
                "tenant_id": tenant_id,
                "display_name": f"Diag P13 T1 {suffix}",
                "default_domain_id": domain_id,
                "default_domain_display_name": "General",
            },
        )
        tj = r.json()
        t1_admin_key = (
            tj.get("admin_raw_key")
            or tj.get("admin_api_key", {}).get("raw_key")
        )
        if not t1_admin_key:
            raise SystemExit(f"onboard returned no admin_raw_key: {tj!r}")
        print(f"[1] tenant onboarded; admin_key prefix = {t1_admin_key[:12]}...")

        # 2. Create platform User U
        r = call(
            "POST", "/api/v1/users", pa, c,
            json={
                "email": user_email,
                "display_name": f"Diag P13 User {suffix}",
                "synthetic": False,
            },
        )
        platform_user_uuid_str = r.json()["id"]
        platform_user_uuid = uuid.UUID(platform_user_uuid_str)
        print(f"[2] platform User U.id = {platform_user_uuid}")

        # 3. Create Agent A1
        r = call(
            "POST", "/api/v1/admin/agents", t1_admin_key, c,
            json={
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "agent_id": agent_a1_slug,
                "display_name": "Diag P13 Agent A1",
                "contact_email": user_email,
            },
        )
        agent_a1_pk = r.json()["id"]
        print(f"[3] agent A1 created (pk={agent_a1_pk}, slug={agent_a1_slug})")

        # 4. Direct DB write: bind a1.user_id = U.id (Pillar 13 pattern)
        from app.db.session import SessionLocal
        from app.models.agent import Agent as AgentModel
        db = SessionLocal()
        try:
            a1 = db.get(AgentModel, agent_a1_pk)
            if a1 is None:
                raise SystemExit("agent disappeared after creation")
            a1.user_id = platform_user_uuid
            db.commit()
            print(f"[4] bound agent.user_id = {platform_user_uuid}")
        finally:
            db.close()

        # 5. Mint chat key K1
        r = call(
            "POST", "/api/v1/admin/api-keys", t1_admin_key, c,
            json={
                "tenant_id": tenant_id,
                "domain_id": domain_id,
                "agent_id": agent_a1_slug,
                "display_name": "Diag P13 K1",
                "permissions": ["chat", "sessions"],
            },
        )
        kb = r.json()
        k1_raw = kb["raw_key"]
        k1_prefix = kb["api_key"]["key_prefix"]
        print(f"[5] K1 minted (prefix={k1_prefix})")

        # 6. Create session
        r = call(
            "POST", "/api/v1/sessions", k1_raw, c,
            json={
                "user_id": end_user_pseudonym,
                "tenant_id": tenant_id,
                "domain_id": domain_id,
            },
        )
        sb = r.json()
        session_id = sb.get("session_id") or sb.get("id")
        print(f"[6] session_id = {session_id}")
        print(f"    session.user_id (string) = {sb.get('user_id')!r}")

        # 7. Consent grant (with the same string pseudonym Pillar 13 uses)
        call(
            "POST", "/api/v1/consent/grant", k1_raw, c,
            json={
                "user_id": sb.get("user_id"),
                "tenant_id": tenant_id,
            },
        )
        print(f"[7] consent granted for user_id={sb.get('user_id')!r}")

        # 8. The exact same legitimate setup-turn message Pillar 13 used
        # (the one we just reverted). We're testing whether THIS message
        # produces a memory_item end-to-end, regardless of text shape.
        sentinel = f"DIAG-LEGIT-{uuid.uuid4().hex[:6].upper()}"
        msg_text = (
            f"Please remember this for future sessions: my account "
            f"verification token is {sentinel}. I will reference this "
            f"token whenever I need to confirm my identity."
        )
        print(f"[8] sending chat with sentinel={sentinel}")
        r = call(
            "POST", "/api/v1/chat", k1_raw, c,
            json={"session_id": session_id, "message": msg_text},
            expect=200,
        )
        reply = r.json().get("reply", "")
        print(f"    chat reply (truncated): {reply[:120]!r}")

    # 9. Wait for worker to process
    print()
    print(f"[9] sleeping 30s for worker to drain...")
    for i in range(6):
        time.sleep(5)
        print(f"    ...{(i+1)*5}s")

    # 10. Dump DB state
    print()
    print("=== DB state after 30s ===")
    from app.db.session import SessionLocal
    from app.models.admin_audit_log import AdminAuditLog
    from app.models.message import MessageModel
    from app.models.memory import MemoryItem
    from sqlalchemy import select

    db = SessionLocal()
    try:
        print(f"\n--- AdminAuditLog rows for tenant={tenant_id} ---")
        rows = db.scalars(
            select(AdminAuditLog)
            .where(AdminAuditLog.tenant_id == tenant_id)
            .order_by(AdminAuditLog.id)
        ).all()
        print(f"  count = {len(rows)}")
        for r in rows:
            note = (r.note or "")[:90]
            print(f"  id={r.id} | {r.created_at} | action={r.action} | "
                  f"actor_prefix={r.actor_key_prefix} | note={note}")

        print(f"\n--- Messages for session={session_id} ---")
        msgs = db.scalars(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.id)
        ).all()
        print(f"  count = {len(msgs)}")
        for m in msgs:
            content = (m.content or "")[:90]
            print(f"  msg_id={m.id} | role={m.role} | content={content!r}")

        print(f"\n--- MemoryItems for tenant={tenant_id} ---")
        mems = db.scalars(
            select(MemoryItem)
            .where(MemoryItem.tenant_id == tenant_id)
            .order_by(MemoryItem.id)
        ).all()
        print(f"  count = {len(mems)}")
        for mi in mems:
            content = (mi.content or "")[:90]
            print(f"  mi_id={mi.id} | message_id={mi.message_id} | "
                  f"actor_user_id={mi.actor_user_id} | "
                  f"content={content!r}")
    finally:
        db.close()

    # 11. Server-side settings introspection
    print(f"\n--- Server settings ---")
    try:
        from app.core.config import settings
        print(f"  settings.memory_extraction_async = "
              f"{settings.memory_extraction_async}")
    except Exception as exc:
        print(f"  could not read settings: {type(exc).__name__}: {exc}")

    print(f"\n=== DONE ===")
    print(f"Tenant slug for log grep: {tenant_id}")
    print(f"Session id for log grep:  {session_id}")
    print(f"Sentinel for memory-item content grep: {sentinel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
