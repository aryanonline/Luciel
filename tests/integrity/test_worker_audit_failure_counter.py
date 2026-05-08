"""Step 29.y gap-fix C4: worker audit-write failure counter + marker.

Drift token: D-worker-audit-write-failure-not-alerted-2026-05-07.

Origin
------
app/worker/tasks/memory_extraction.py had a silent
``except Exception: logger.exception(...)`` around audit-write
inside the rejection path. The log line had no structured marker
an operator could count or alert on.

This module asserts:

  1. The WORKER_AUDIT_WRITE_FAILED marker constant exists and is a
     stable plain string -- not an f-string template.
  2. record_audit_write_failure() returns a monotonically
     increasing integer, starting from the current value.
  3. current_audit_write_failure_count() reflects increments.
  4. reset_audit_write_failure_count() zeroes the counter.
  5. Concurrent increments from multiple threads do not lose
     updates (lock works).
  6. AST: memory_extraction.py imports the marker constant AND the
     record function, AND uses both inside the except block. This
     prevents a refactor from accidentally reverting the audit
     failure path back to a silent log line.
"""
from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path

from app.worker.audit_failure_counter import (
    WORKER_AUDIT_WRITE_FAILED,
    current_audit_write_failure_count,
    record_audit_write_failure,
    reset_audit_write_failure_count,
)


# 1. Marker is a stable plain string

def test_marker_constant_is_plain_string():
    assert isinstance(WORKER_AUDIT_WRITE_FAILED, str)
    assert WORKER_AUDIT_WRITE_FAILED == "WORKER_AUDIT_WRITE_FAILED"
    # No f-string interpolation tokens
    assert "{" not in WORKER_AUDIT_WRITE_FAILED
    assert "%" not in WORKER_AUDIT_WRITE_FAILED


# 2. Counter increments monotonically

def test_counter_increments_monotonically():
    reset_audit_write_failure_count()
    assert current_audit_write_failure_count() == 0
    assert record_audit_write_failure() == 1
    assert record_audit_write_failure() == 2
    assert record_audit_write_failure() == 3
    assert current_audit_write_failure_count() == 3


# 3. Reset

def test_reset_zeroes_counter():
    reset_audit_write_failure_count()
    record_audit_write_failure()
    record_audit_write_failure()
    assert current_audit_write_failure_count() == 2
    reset_audit_write_failure_count()
    assert current_audit_write_failure_count() == 0


# 4. Concurrency: lock holds under thread storm

def test_counter_thread_safe_under_concurrent_increments():
    reset_audit_write_failure_count()
    n_threads = 16
    increments_per_thread = 200
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # maximize contention
        for _ in range(increments_per_thread):
            record_audit_write_failure()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * increments_per_thread
    assert current_audit_write_failure_count() == expected, (
        "Lost updates under concurrent increment -- the lock in "
        "audit_failure_counter.record_audit_write_failure is broken."
    )
    reset_audit_write_failure_count()


# 5. AST: memory_extraction.py wires both marker AND record fn into
#    its except block

def test_memory_extraction_wires_marker_and_counter():
    """Pin the wiring at the source level. A future refactor that
    drops either the marker or the counter call regresses the gap-fix.
    """
    from app.worker.tasks import memory_extraction
    src = Path(memory_extraction.__file__).read_text()

    # Direct import line presence
    assert "from app.worker.audit_failure_counter import" in src
    assert "WORKER_AUDIT_WRITE_FAILED" in src
    assert "record_audit_write_failure" in src

    # AST: the call to record_audit_write_failure must appear inside
    # an ExceptHandler body somewhere in the module.
    tree = ast.parse(src)
    found_in_except = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    name = (
                        func.id if isinstance(func, ast.Name)
                        else getattr(func, "attr", None)
                    )
                    if name == "record_audit_write_failure":
                        found_in_except = True
                        break
            if found_in_except:
                break
    assert found_in_except, (
        "record_audit_write_failure must be called inside an "
        "except: block in memory_extraction.py."
    )


def test_marker_used_in_log_line():
    """The marker must appear in the same source as a logger call
    so the structured log line is intact.
    """
    from app.worker.tasks import memory_extraction
    src = Path(memory_extraction.__file__).read_text()
    # Find any logger call that references WORKER_AUDIT_WRITE_FAILED
    # in its argument list.
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = getattr(func, "attr", None)
            if attr in ("exception", "error", "warning", "critical", "info"):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == "WORKER_AUDIT_WRITE_FAILED":
                        found = True
                        break
            if found:
                break
    assert found, (
        "A logger call must include WORKER_AUDIT_WRITE_FAILED as "
        "an argument so log aggregators can pin on the marker."
    )
