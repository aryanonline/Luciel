"""Worker audit-write failure counter and structured log marker.

Step 29.y gap-fix C4 (D-worker-audit-write-failure-not-alerted-2026-05-07).

Origin
------
app/worker/tasks/memory_extraction.py contained a silent
``except Exception: logger.exception(...)`` around the audit-write
inside the rejection path (line 169 pre-fix). Rationale was sound --
audit-write failure must never mask the original rejection -- but
the log line had no structured marker an operator could count or
alert on. A run-of-the-mill log noise spike could hide an
audit-write failure storm.

What this module provides
-------------------------
- ``WORKER_AUDIT_WRITE_FAILED`` -- a stable structured marker string
  the worker writes into its log line on every audit-write failure.
  CloudWatch metric filters (Step 30b prod work) and future Prom
  exporters can pin on this exact string. It is a CONSTANT, not an
  f-string template, so log-aggregator regex rules stay stable
  across refactors.

- ``record_audit_write_failure()`` -- thread-safe in-process counter
  increment. Returns the post-increment value so the caller can log
  it on the same line (\"failure #7 in this worker process\"). The
  counter is process-local; aggregation across worker pods is the
  operability layer's job (Step 30b alarm wiring), not ours.

- ``current_audit_write_failure_count()`` -- read accessor. Used by
  tests and any future health endpoint.

- ``reset_audit_write_failure_count()`` -- only for tests. Workers
  do not call this in normal operation.

Concurrency
-----------
Celery's prefork worker model has multiple processes per pod; each
process has its own counter. Within a process, multiple threads
(e.g. a thread pool inside a single worker) share the counter, so
we guard the read-modify-write with a lock. The Python GIL would
make a bare ``+= 1`` atomic in CPython today, but relying on that
is the kind of implicit assumption Luciel's working doctrine
specifically rejects.

Operability hook surface
------------------------
A future commit can wire ``record_audit_write_failure`` into a
Prometheus Counter or a CloudWatch PutMetricData call. Today's
in-process counter is the minimum viable hook: it lets us assert
in tests that the counter ticks on failure, and lets a future
health endpoint report \"this worker has seen N audit-write
failures since boot.\"
"""
from __future__ import annotations

import threading


# Structured log marker. Stable string the operability layer pins on.
# DO NOT change without updating any CloudWatch / Prom rules that
# reference it -- that's a coordinated prod change.
WORKER_AUDIT_WRITE_FAILED = "WORKER_AUDIT_WRITE_FAILED"


# Process-local counter state.
_lock = threading.Lock()
_count = 0


def record_audit_write_failure() -> int:
    """Increment the failure counter and return the new value.

    Thread-safe within a single Python process. Cross-process
    aggregation is the operability layer's job.
    """
    global _count
    with _lock:
        _count += 1
        return _count


def current_audit_write_failure_count() -> int:
    """Read the current counter value. Test helper / health hook."""
    with _lock:
        return _count


def reset_audit_write_failure_count() -> None:
    """Reset the counter to zero. Test-only -- workers must not
    call this in normal operation.
    """
    global _count
    with _lock:
        _count = 0
