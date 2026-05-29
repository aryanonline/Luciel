"""Always-on cognition module — Arc 12 WU7 interim host.

================================================================
TODO(ARC14): This module is the INTERIM HOST for the three
always-on cognition behaviours — escalate (human handoff),
save_memory (deliberate fact persistence), and session_summary
(conversation recap). It is INTENTIONALLY MINIMAL: behaviour
PRESERVED, NOT EXPANDED. Per Architecture §3.4 cognition is
always-on, every tier, never admin-configurable, and per Decision
#20 it is NOT in the tool registry.

The permanent home for cognition is the Arc 14 agentic loop
(``app.runtime.orchestrator.LucielOrchestrator.run``). When that
loop subsumes PLAN / ACT / REFLECT, the behaviours here are
absorbed into it. Absorption is an Arc 14 EXIT CRITERION; this
module disappears at that point.

Founder ruling 4 (Arc 12):
  4a — interim-marked + ARC-14-tracked  ✓ (this header + the
       TODO(ARC14) markers below)
  4b — behaviour-preserving, NOT behaviour-expanding. Do not
       extend, improve, or re-architect any cognition behaviour
       in this module. Cognition redesign is Arc 14.
  4c — minimal & non-tier-gated. NO broker, NO registry, NO
       tier-gating, NO shadow agentic loop. Called DIRECTLY by
       chat_service.

================================================================
"""
from __future__ import annotations

from app.cognition.service import CognitionOutcome, CognitionService

__all__ = ["CognitionOutcome", "CognitionService"]
