"""
Luciel async worker subpackage (Step 27b).

Runs out-of-band tasks on a separate ECS service (`luciel-worker`) so the
chat request path stays fast and isolated from bursty CPU-bound work.

Entrypoint (ECS task-def command):
    celery -A app.worker.celery_app worker --loglevel=info --concurrency=2

Security & Invariant Contract:
    docs/runbooks/step-27b-security-contract.md

The FastAPI web process imports only `enqueue_*` helpers from
`app.memory.service`. It never imports Celery directly. Celery imports
inside those helpers are lazy so the web process carries no broker
connection and no task-registry reflection.

Domain-agnosticism: this subpackage contains no vertical strings,
no hardcoded tenant ids, and no imports from `app.domain`. Tasks
operate on opaque ids only.
"""