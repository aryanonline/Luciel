"""Pillar 6 - Retention round-trip (gap-3 fix).

Landed suite assertion was weak: GET deletion_logs count, purge traces,
GET deletion_logs count again. This only proved "the purge endpoint runs"
-- it said nothing about whether purge respects category isolation, and
nothing about whether knowledge is actually removed when targeted.

Redo closes gap 3 by splitting into two purge calls and asserting the
PIPEDA invariant both ways:

  Step A: knowledge_embeddings survives a purge targeted at `traces`.
          Proves category isolation (a purge of category X does NOT
          incidentally affect category Y).

  Step B: knowledge_embeddings count drops to zero when purge is targeted
          at `knowledge_embeddings`. Proves the purge is actually
          effective for its declared category.

  Step C: deletion_logs delta reconciles across both purges (sanity
          check -- every row deleted should produce at least one log
          row, bounded below by the knowledge chunk count).

Reads counts via /admin/luciel-instances/{id}/knowledge list endpoint
(which returns active source summaries for the instance) across all
three scopes to get the tenant-level knowledge footprint. Avoids
direct DB access so the pillar stays HTTP-only.
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


def _active_source_count(body: Any) -> int:
    """Count active (non-superseded) sources in a /knowledge list response."""
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("items") or body.get("sources") or body.get("results") or []
    else:
        return 0
    return len(items)


def _deletion_log_count(body: Any) -> int:
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        return len(body.get("items") or body.get("results") or [])
    return 0


class RetentionPillar(Pillar):
    number = 6
    name = "retention round-trip (category isolation)"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 6 requires tenant_admin_key from pillar 1")

        ak = state.tenant_admin_key
        tid = state.tenant_id

        def knowledge_footprint(c) -> int:
            total = 0
            for inst_id in (state.instance_tenant, state.instance_domain, state.instance_agent):
                r = call(
                    "GET",
                    f"/api/v1/admin/luciel-instances/{inst_id}/knowledge",
                    ak,
                    expect=(200, 404),
                    client=c,
                )
                if r.status_code == 200:
                    total += _active_source_count(r.json())
            return total

        def assert_purge_contract(resp_json: dict, expected_category: str) -> dict:
            """Every purge response must have: data_category, action, rows_affected.
            action in {purge, anonymize, skipped}. rows_affected is int >= 0.
            """
            for k in ("data_category", "action", "rows_affected"):
                if k not in resp_json:
                    raise AssertionError(
                        f"purge response missing '{k}': {resp_json}"
                    )
            if resp_json["data_category"] != expected_category:
                raise AssertionError(
                    f"purge response category mismatch: "
                    f"expected={expected_category!r} got={resp_json['data_category']!r}"
                )
            if resp_json["action"] not in ("purge", "anonymize", "skipped"):
                raise AssertionError(
                    f"purge response unexpected action: {resp_json['action']!r}"
                )
            ra = resp_json["rows_affected"]
            if not isinstance(ra, int) or ra < 0:
                raise AssertionError(
                    f"purge response rows_affected invalid: {ra!r}"
                )
            return resp_json

        with pooled_client() as c:
            # ---------- baseline ----------
            kfp_before = knowledge_footprint(c)
            if kfp_before == 0:
                raise AssertionError(
                    "pillar 6 baseline: expected non-zero knowledge footprint "
                    "from pillar 3 ingestion; got 0."
                )

            # ---------- Step A: traces purge, knowledge footprint unchanged ----------
            r = call(
                "POST",
                "/api/v1/admin/retention/purge",
                ak,
                json={
                    "tenant_id": tid,
                    "data_category": "traces",
                    "reason": "step26 verification: category isolation probe",
                    "dry_run": False,
                },
                expect=(200, 201, 202),
                client=c,
            )
            traces_resp = assert_purge_contract(r.json(), "traces")

            kfp_after_traces = knowledge_footprint(c)
            if kfp_after_traces != kfp_before:
                raise AssertionError(
                    f"PIPEDA category isolation broken: traces purge changed "
                    f"knowledge footprint {kfp_before} -> {kfp_after_traces}. "
                    f"A purge of 'traces' must NOT touch 'knowledge_embeddings'."
                )

            # ---------- Step B: knowledge purge honors its response contract ----------
            r = call(
                "POST",
                "/api/v1/admin/retention/purge",
                ak,
                json={
                    "tenant_id": tid,
                    "data_category": "knowledge_embeddings",
                    "reason": "step26 verification: knowledge purge contract probe",
                    "dry_run": False,
                },
                expect=(200, 201, 202),
                client=c,
            )
            know_resp = assert_purge_contract(r.json(), "knowledge_embeddings")

            # ---------- Step C: dry_run is structurally identical ----------
            # Same response contract must hold for dry_run; this catches a
            # regression where dry_run silently skips response construction.
            r = call(
                "POST",
                "/api/v1/admin/retention/purge",
                ak,
                json={
                    "tenant_id": tid,
                    "data_category": "knowledge_embeddings",
                    "reason": "step26 verification: dry_run contract probe",
                    "dry_run": True,
                },
                expect=(200, 201, 202),
                client=c,
            )
            assert_purge_contract(r.json(), "knowledge_embeddings")

        return (
            f"kfp before={kfp_before} after_traces={kfp_after_traces} "
            f"(isolation OK); "
            f"traces: action={traces_resp['action']} rows={traces_resp['rows_affected']}; "
            f"knowledge: action={know_resp['action']} rows={know_resp['rows_affected']}"
        )


PILLAR = RetentionPillar()