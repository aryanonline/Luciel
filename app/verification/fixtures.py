"""Step 26 verification fixtures.

Contents:
  - RunState dataclass (carries state across pillars)
  - Platform-admin key loader (env-driven, with actionable error msg)
  - Throwaway tenant-id generator (step26-verify-<uuid8>)
  - Sample documents: SAMPLE_MD, SAMPLE_CSV
  - make_synthetic_pdf(version, embed_token) -- gap-1 fix: token is
    embedded into the PDF content so pillar 5's retrieval assertion
    tests real round-trip, not a phantom sentinel
  - Sentinel constants: PDF_SENTINEL_V1, PDF_SENTINEL_V2, MD_SENTINEL
  - sweep_residue_tenants() -- hard-deactivates leftover step26-verify-*
    tenants older than a cutoff (default 1h). Gap-8 partial fix; pillar 10
    provides the assertion layer.

Sentinels are deliberately distinct per document so pillar 5 can prove
scope-correct retrieval (gap 6): agent-bound chat surfaces PDF_SENTINEL_V2,
domain-bound chat surfaces MD_SENTINEL, neither leaks across.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT, call, h, pooled_client


# ---------- sentinels (embedded in fixtures, asserted in pillar 5) ----------

PDF_SENTINEL_V1 = "MAGICTOKEN-PDF-V1"
PDF_SENTINEL_V2 = "MAGICTOKEN-PDF-V2"
MD_SENTINEL = "MAGICTOKEN-MD"


# ---------- platform admin key loader ----------

def load_platform_admin_key() -> str:
    """Load from env; exit with actionable msg if missing."""
    key = os.environ.get("LUCIEL_PLATFORM_ADMIN_KEY")
    if not key:
        print(
            "FATAL: LUCIEL_PLATFORM_ADMIN_KEY env var not set.\n"
            "  Set it to a platform_admin bearer (e.g. the luc_sk_rWQ0a... key)\n"
            "  and re-run: python -m app.verification",
            file=sys.stderr,
        )
        sys.exit(2)
    if not isinstance(key, str) or len(key) < 20:
        print(f"FATAL: LUCIEL_PLATFORM_ADMIN_KEY looks malformed: {key!r}", file=sys.stderr)
        sys.exit(2)
    return key


def new_tenant_id() -> str:
    """Fresh throwaway tenant id per run."""
    return f"step26-verify-{uuid.uuid4().hex[:8]}"


# ---------- RunState: passed through every pillar ----------

@dataclass
class RunState:
    """Mutable state carried across pillars.

    Pillars read what they need, write what they produce. The runner
    owns the RunState instance for the duration of the suite.
    """
    tenant_id: str = field(default_factory=new_tenant_id)
    platform_admin_key: str = field(default_factory=load_platform_admin_key)

    # pillar 1 -> tenant admin key for the throwaway tenant
    tenant_admin_key: str | None = None

    # pillar 2 -> scope hierarchy ids
    domain_id: str | None = None
    agent_id: str | None = None
    instance_tenant: int | None = None
    instance_domain: int | None = None
    instance_agent: int | None = None
    # pillar 2 (gap-5 fix): mint agent-scoped admin key *before* cascade
    # so pillar 8's above-scope negative test is unconditional
    agent_admin_key: str | None = None
    agent_admin_key_id: int | None = None

    # pillar 3 -> source ids for versioning round-trip
    source_id_pdf: str | None = None
    source_id_md: str | None = None
    source_id_csv: str | None = None

    # pillar 4 -> chat keys bound to each scope level
    chat_keys: list[dict[str, Any]] = field(default_factory=list)
    # {"key": raw, "instance_id": int, "id": int, "scope_level": "tenant"|"domain"|"agent"}

    # teardown ledger
    keys_to_deactivate: list[int] = field(default_factory=list)

    def chat_key_for(self, instance_id: int) -> dict[str, Any] | None:
        for ck in self.chat_keys:
            if ck.get("instance_id") == instance_id:
                return ck
        return None


# ---------- sample documents (domain-agnostic -- real-estate flavor is data, not code) ----------

SAMPLE_MD = f"""# Crossroads Domain Brief

## Commission Structure
Standard commission is 2.5% listing side, 2.5% buyer side.
Luxury properties over $2M carry a 3% structure.

## Domain-brief token
{MD_SENTINEL}

## Escalation
Any corporate-buyer offer must be escalated to the Sales Director.
"""

SAMPLE_CSV = """mls_id,address,price,beds,baths
MLS001,123 Maple St Markham,1250000,4,3
MLS002,45 Birch Ave Richmond Hill,985000,3,2
MLS003,88 Oak Rd Vaughan,1780000,5,4
"""


def make_synthetic_pdf(*, version: int = 1, embed_token: str | None = None) -> bytes:
    """Generate a reportlab PDF with an embedded sentinel token.

    Gap-1 fix: the landed suite defined MAGIC_TOKEN='PURPLE-OWL-42-STEP26'
    but never embedded it in any ingested document, so pillar 5 could only
    pass by LLM hallucination. Here the token is part of the PDF body, so
    retrieval is a genuine round-trip.

    Defaults: version=1 embeds PDF_SENTINEL_V1; version=2 embeds
    PDF_SENTINEL_V2. Pass embed_token=... to override for edge-case tests.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "reportlab not installed. Run: pip install reportlab"
        )

    if embed_token is None:
        embed_token = PDF_SENTINEL_V2 if version == 2 else PDF_SENTINEL_V1

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 720, f"REMAX Crossroads Listings v{version}")
    c.setFont("Helvetica", 11)
    y = 680
    lines = [
        f"Prepared for Step 26 verification (version {version}).",
        f"Secret verification token: {embed_token}",
        "",
        "Listing 1: 123 Maple St Markham $1,250,000 4bed/3bath",
        "Listing 2: 45 Birch Ave Richmond Hill $985,000 3bed/2bath",
        "Listing 3: 88 Oak Rd Vaughan $1,780,000 5bed/4bath",
        "All listings MLS-verified April 2026.",
    ]
    for ln in lines:
        c.drawString(72, y, ln)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------- residue sweep (gap-8 partial fix; pillar 10 asserts) ----------

def sweep_residue_tenants(
    *,
    platform_admin_key: str,
    prefix: str = "step26-verify-",
    older_than: timedelta = timedelta(hours=1),
) -> dict[str, Any]:
    """Hard-deactivate leftover step26-verify-* tenants.

    Safe by design: only touches tenants matching `prefix` AND older than
    `older_than` cutoff, so an in-flight run's tenant is never swept.

    Returns a summary dict: {swept: [tenant_ids], skipped: [tenant_ids], errors: [...]}.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    summary: dict[str, Any] = {"swept": [], "skipped": [], "errors": []}

    with pooled_client() as c:
        r = call("GET", "/api/v1/admin/tenants", platform_admin_key, expect=200, client=c)
        body = r.json()
        items = body if isinstance(body, list) else body.get("value", body.get("items", []))

        for t in items:
            tid = t.get("tenant_id", "")
            if not tid.startswith(prefix):
                continue
            if not t.get("active", False):
                summary["skipped"].append(tid)  # already inactive
                continue
            created_raw = t.get("created_at")
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except Exception:
                summary["errors"].append(f"{tid}: unparseable created_at={created_raw!r}")
                continue
            if created > cutoff:
                summary["skipped"].append(tid)  # too fresh; could be in-flight
                continue
            try:
                call(
                    "PATCH",
                    f"/api/v1/admin/tenants/{tid}",
                    platform_admin_key,
                    json={"active": False},
                    expect=(200, 404),
                    client=c,
                )
                summary["swept"].append(tid)
            except Exception as exc:
                summary["errors"].append(f"{tid}: {exc}")

    return summary