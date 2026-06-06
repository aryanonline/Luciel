"""
Celery task registry for the Luciel worker.

Task modules are auto-discovered via the `include` list in
`app.worker.celery_app.celery_app`. Adding a new task module =
add it to that `include` list AND import it here.
"""
from app.worker.tasks import memory_extraction  # noqa: F401
from app.worker.tasks import retention  # noqa: F401  # Step 30a.2
# escalation_chain_walker removed (Unit 1 excision) -- Enterprise
# escalation chains are deferred; delivery is a flat per-signal map
# (Architecture §3.5.3). No SLA chain-advance task in the Free/Pro model.

__all__ = ["memory_extraction", "retention"]