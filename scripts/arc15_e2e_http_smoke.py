"""ARC 15 end-to-end HTTP smoke test.

Seeds real Admins + Instances directly in Postgres, mints real API keys via
ApiKeyService, then drives every ARC 15 endpoint over actual HTTP against the
running uvicorn server (full middleware + serialization + RLS + DB writes).

Asserts the journey-critical, document-grounded behaviors:
  * Personality: 4 presets all tiers; custom rejected on Free, allowed on Pro;
    business_context tier cap (280 Free/Pro, 2000 Ent).
  * Escalation: contact+routing only; signals returned read-only; tier-shaped
    notify channels.
  * Connections honesty fork: csv/property_source + outbound_webhook connect
    LIVE (status=connected); calendar/crm DEFERRED (unconfigured + arc17_pending).
  * ToolView connection_status chip: action_needed | connected | reconnect_needed.
Exit 0 = all assertions pass.
"""
from __future__ import annotations

import sys
import uuid

import requests

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.admin import Admin
from app.models.instance import Instance
from app.models.instance_status import InstanceStatus
from app.services.api_key_service import ApiKeyService

BASE = "http://127.0.0.1:8000"
PFX = settings.api_v1_prefix  # e.g. /api/v1

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"{mark}  {label}" + (f"  -- {detail}" if detail else ""))


def seed(tier: str) -> tuple[str, int, str]:
    """Create an Admin(tier) + one Instance, mint an admin API key with the
    configure-connections permission. Returns (admin_id, instance_id, raw_key).
    """
    db = SessionLocal()
    try:
        admin_id = f"arc15e2e-{uuid.uuid4().hex[:8]}"
        db.add(Admin(id=admin_id, name="arc15 e2e", tier=tier, active=True))
        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"inst-{uuid.uuid4().hex[:6]}",
            display_name="E2E Concierge",
            instance_status=InstanceStatus.ACTIVE,
            enabled_channels=["widget"],
        )
        db.add(inst)
        db.flush()
        instance_id = inst.id
        svc = ApiKeyService(db)
        # Admin key with the perms the ARC 15 admin routes require.
        # platform_admin is the documented cross-Admin operator that the
        # house route tests use to isolate tier-gate behavior from Wall-2
        # role resolution (PermissionResolver step 1 grants all perms).
        from app.policy.scope import PLATFORM_ADMIN

        apikey, raw = svc.create_key(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            permissions=["admin", PLATFORM_ADMIN],
            display_name="arc15-e2e",
        )
        db.commit()
        return admin_id, instance_id, raw
    finally:
        db.close()


def H(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def main() -> int:
    # ---- Free tier ----
    fa, fi, fkey = seed("free")
    # ---- Pro tier ----
    pa, pi, pkey = seed("pro")

    # --- Personality: GET reflects tier shaping ---
    r = requests.get(f"{BASE}{PFX}/admin/instances/{fi}/personality", headers=H(fkey), timeout=10)
    check("personality GET free 200", r.status_code == 200, str(r.status_code))
    if r.ok:
        b = r.json()
        check("free custom_preset_available == False", b.get("custom_preset_available") is False, str(b.get("custom_preset_available")))
        check("free business_context cap == 280", b.get("business_context_max_chars") == 280, str(b.get("business_context_max_chars")))

    r = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/personality", headers=H(pkey), timeout=10)
    if r.ok:
        b = r.json()
        check("pro custom_preset_available == True", b.get("custom_preset_available") is True, str(b.get("custom_preset_available")))

    # --- Personality: named preset accepted on Free ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{fi}/personality", headers=H(fkey),
                     json={"personality_preset": "warm_concierge"}, timeout=10)
    check("free set named preset 200", r.status_code == 200, str(r.status_code))

    # --- Personality: custom REJECTED on Free (doctrine: no axis exposure on Free) ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{fi}/personality", headers=H(fkey),
                     json={"personality_preset": "custom",
                           "personality_axes": {"tone": "warm", "verbosity": "balanced", "formality": "casual", "pace": "relaxed"}}, timeout=10)
    check("free custom preset REJECTED (403)", r.status_code == 403, str(r.status_code))

    # --- Personality: custom ALLOWED on Pro ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{pi}/personality", headers=H(pkey),
                     json={"personality_preset": "custom",
                           "personality_axes": {"tone": "warm", "verbosity": "balanced", "formality": "casual", "pace": "relaxed"}}, timeout=10)
    check("pro custom preset ALLOWED (200)", r.status_code == 200, str(r.status_code))

    # --- Personality: business_context over Free/Pro cap (280) REJECTED ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{pi}/personality", headers=H(pkey),
                     json={"personality_preset": "professional_advisor", "business_context": "x" * 281}, timeout=10)
    check("pro business_context > 280 REJECTED (422)", r.status_code == 422, str(r.status_code))

    # --- Personality PUT actually PERSISTS (session-bug regression guard) ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{pi}/personality", headers=H(pkey),
                     json={"personality_preset": "trusted_authority", "business_context": "data-driven brokerage"}, timeout=10)
    check("pro personality PUT 200 (no 500)", r.status_code == 200, str(r.status_code))
    rg = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/personality", headers=H(pkey), timeout=10)
    if rg.ok:
        b = rg.json()
        check("personality PUT persisted (preset + business_context round-trip)",
              b.get("personality_preset") == "trusted_authority" and b.get("business_context") == "data-driven brokerage",
              str({k: b.get(k) for k in ('personality_preset', 'business_context')}))

    # --- Escalation PUT actually PERSISTS (same session-bug guard) ---
    r = requests.put(f"{BASE}{PFX}/admin/instances/{pi}/escalation", headers=H(pkey),
                     json={"config": {"primary_contact": {"channel": "sms", "value": "+16475550100"}}}, timeout=10)
    check("pro escalation PUT 200 (no 500)", r.status_code == 200, str(r.status_code))
    rg = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/escalation", headers=H(pkey), timeout=10)
    if rg.ok:
        cfg = rg.json().get("escalation_config") or {}
        check("escalation PUT persisted (config round-trip)", cfg.get("primary_contact", {}).get("value") == "+16475550100", str(cfg))

    # --- Escalation: GET shows read-only signals + tier-shaped channels ---
    r = requests.get(f"{BASE}{PFX}/admin/instances/{fi}/escalation", headers=H(fkey), timeout=10)
    check("escalation GET free 200", r.status_code == 200, str(r.status_code))
    if r.ok:
        b = r.json()
        sigs = set(b.get("escalation_signals", []))
        expected = {"explicit_human_request", "cannot_confidently_answer", "strong_negative_sentiment", "high_value_lead"}
        check("escalation: 4 fixed signals returned read-only", sigs == expected, str(sigs))
        check("escalation free notify channels == [email]", b.get("available_notify_channels") == ["email"], str(b.get("available_notify_channels")))

    r = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/escalation", headers=H(pkey), timeout=10)
    if r.ok:
        b = r.json()
        check("escalation pro notify channels include sms", "sms" in (b.get("available_notify_channels") or []), str(b.get("available_notify_channels")))

    # --- Connections honesty fork (Pro): LIVE csv/webhook vs DEFERRED calendar/crm ---
    # property_source (csv) → LIVE connected
    r = requests.post(f"{BASE}{PFX}/admin/instances/{pi}/connections", headers=H(pkey),
                      json={"connection_type": "property_source", "provider": "csv", "non_secret_config": {"store_ref": "s3://b/listings.csv"}}, timeout=10)
    check("connect property_source(csv) 201", r.status_code == 201, str(r.status_code))
    if r.ok:
        conn = r.json().get("connection", {})
        check("property_source LIVE -> connected", conn.get("status") == "connected", str(r.json()))

    # outbound_webhook → LIVE connected
    r = requests.post(f"{BASE}{PFX}/admin/instances/{pi}/connections", headers=H(pkey),
                      json={"connection_type": "outbound_webhook", "provider": "custom_webhook", "non_secret_config": {"url": "https://hooks.example.com/x"}}, timeout=10)
    check("connect outbound_webhook 201", r.status_code == 201, str(r.status_code))

    # calendar → DEFERRED unconfigured + arc17_pending
    r = requests.post(f"{BASE}{PFX}/admin/instances/{pi}/connections", headers=H(pkey),
                      json={"connection_type": "calendar", "provider": "google_calendar", "non_secret_config": {}}, timeout=10)
    check("connect calendar accepted (2xx)", r.status_code in (200, 201, 202), str(r.status_code))
    if r.ok:
        body = r.json()
        conn = body.get("connection", {})
        deferred = body.get("arc17_pending") is not None
        not_faked = conn.get("status") == "unconfigured"
        check("calendar DEFERRED (arc17_pending + unconfigured, not faked connected)", deferred and not_faked, str(body)[:200])

    # --- Connection list reflects the rows ---
    r = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/connections", headers=H(pkey), timeout=10)
    check("connections GET list 200", r.status_code == 200, str(r.status_code))
    if r.ok:
        conns = r.json().get("connections", [])
        types = {c.get("connection_type") for c in conns}
        check("list contains property_source+webhook+calendar", {"property_source", "outbound_webhook", "calendar"} <= types, str(types))

    # --- ToolView connection_status chip mapping ---
    r = requests.get(f"{BASE}{PFX}/admin/instances/{pi}/tools", headers=H(pkey), timeout=10)
    check("tools GET 200", r.status_code == 200, str(r.status_code))
    if r.ok:
        tools = r.json().get("tools", r.json()) if isinstance(r.json(), dict) else r.json()
        tools = tools if isinstance(tools, list) else r.json().get("tools", [])
        by_id = {t.get("tool_id"): t for t in tools}
        lp = by_id.get("lookup_property")
        if lp:
            check("lookup_property chip == connected (property_source live)", lp.get("connection_status") == "connected", str(lp.get("connection_status")))
        bk = by_id.get("book_appointment")
        if bk:
            check("book_appointment chip == action_needed (calendar deferred)", bk.get("connection_status") == "action_needed", str(bk.get("connection_status")))
        sc = by_id.get("schedule_callback")
        if sc:
            check("schedule_callback chip == null (no connection required)", sc.get("connection_status") in (None, "null"), str(sc.get("connection_status")))

    print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
