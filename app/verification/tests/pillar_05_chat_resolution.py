"""Pillar 5 - Chat resolution with scope-correct retrieval (gaps 1 + 6).

Asserts the full chat pipeline -- persona resolution, LucielInstance
binding, knowledge retrieval, LLM generation -- via real LLM turns
against live scope-bound chat keys.

Landed suite gap 1: MAGIC_TOKEN='PURPLE-OWL-42-STEP26' was never embedded
in any ingested fixture, so pillar 5 could only pass by LLM hallucination.
Redo fix: the PDF fixture embeds PDF_SENTINEL_V2 post-replace (written
in via pillar 3 step 6), the MD fixture embeds MD_SENTINEL. Pillar 5
asserts each sentinel appears in the reply from its corresponding
scope-bound key.

Landed suite gap 6: no positive test that domain-bound chat returns
domain knowledge (and not, say, the agent's private PDF). Redo adds a
second LLM turn against the domain-bound chat key, asserting MD_SENTINEL
surfaces -- proving scope-correct retrieval, not just "retrieval works".

Two turns:
  Turn A  agent-bound key  -> asks about listings PDF -> expect PDF_SENTINEL_V2
  Turn B  domain-bound key -> asks about domain brief  -> expect MD_SENTINEL

Each turn:
  1. POST /api/v1/sessions  (binds to tenant/domain/agent triple)
  2. POST /api/v1/chat      (sessions + message, returns reply)
  3. Assert expected sentinel in reply; assert OTHER sentinel is NOT in
     the same reply (cross-scope non-leak check).
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import (
    MD_SENTINEL,
    PDF_SENTINEL_V2,
    RunState,
)
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


def _extract_session_id(j: dict[str, Any]) -> str:
    sid = j.get("session_id") or j.get("id") or j.get("sessionId")
    if not isinstance(sid, str) or not sid:
        raise AssertionError(f"session create did not return session_id: {j}")
    return sid


def _extract_reply(j: dict[str, Any]) -> str:
    return j.get("reply") or j.get("content") or j.get("message") or ""


class ChatResolutionPillar(Pillar):
    number = 5
    name = "chat resolution (scope-correct LLM round-trip)"

    def run(self, state: RunState) -> str:
        if not state.chat_keys:
            raise AssertionError("pillar 5 requires chat_keys from pillar 4")

        agent_ck = state.chat_key_for(state.instance_agent)
        domain_ck = state.chat_key_for(state.instance_domain)
        if agent_ck is None:
            raise AssertionError("pillar 5: agent-bound chat key missing from state")
        if domain_ck is None:
            raise AssertionError("pillar 5: domain-bound chat key missing from state")

        tid = state.tenant_id
        results: list[str] = []

        with pooled_client() as c:
            # ---------- Turn A: agent-bound, expect PDF_SENTINEL_V2 ----------
            r = call(
                "POST",
                "/api/v1/sessions",
                agent_ck["key"],
                json={
                    "user_id": "step26-user-agent",
                    "tenant_id": tid,
                    "domain_id": state.domain_id,
                    "agent_id": state.agent_id,
                },
                expect=(200, 201),
                client=c,
            )
            sess_a = _extract_session_id(r.json())

            r = call(
                "POST",
                "/api/v1/chat",
                agent_ck["key"],
                json={
                    "session_id": sess_a,
                    "message": (
                        "What is the secret verification token in the listings "
                        "document? Reply with just the token and nothing else."
                    ),
                },
                expect=200,
                client=c,
            )
            reply_a = _extract_reply(r.json())
            if PDF_SENTINEL_V2 not in reply_a:
                raise AssertionError(
                    f"agent-bound Luciel did not surface {PDF_SENTINEL_V2!r}. "
                    f"reply[:300]={reply_a[:300]!r}"
                )
            # cross-scope non-leak: agent reply must NOT contain domain sentinel
            if MD_SENTINEL in reply_a:
                raise AssertionError(
                    f"agent-bound reply leaked domain sentinel {MD_SENTINEL!r}: "
                    f"{reply_a[:300]!r}"
                )
            results.append(f"agent->PDF_SENTINEL_V2 OK ({len(reply_a)}ch)")

            # ---------- Turn B: domain-bound, expect MD_SENTINEL ----------
            r = call(
                "POST",
                "/api/v1/sessions",
                domain_ck["key"],
                json={
                    "user_id": "step26-user-domain",
                    "tenant_id": tid,
                    "domain_id": state.domain_id,
                },
                expect=(200, 201),
                client=c,
            )
            sess_b = _extract_session_id(r.json())

            r = call(
                "POST",
                "/api/v1/chat",
                domain_ck["key"],
                json={
                    "session_id": sess_b,
                    "message": (
                        "What is the domain-brief token in the Crossroads domain "
                        "brief? Reply with just the token and nothing else."
                    ),
                },
                expect=200,
                client=c,
            )
            reply_b = _extract_reply(r.json())
            if MD_SENTINEL not in reply_b:
                raise AssertionError(
                    f"domain-bound Luciel did not surface {MD_SENTINEL!r}. "
                    f"reply[:300]={reply_b[:300]!r}"
                )
            # cross-scope non-leak: domain reply must NOT contain agent-private
            # PDF sentinel (agent knowledge is below domain scope, should be
            # invisible at the domain level).
            if PDF_SENTINEL_V2 in reply_b:
                raise AssertionError(
                    f"domain-bound reply leaked agent-private sentinel "
                    f"{PDF_SENTINEL_V2!r}: {reply_b[:300]!r}"
                )
            results.append(f"domain->MD_SENTINEL OK ({len(reply_b)}ch)")

        return "; ".join(results)


PILLAR = ChatResolutionPillar()