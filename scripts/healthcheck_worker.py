#!/usr/bin/env python3
"""Container HEALTHCHECK probe for the Luciel Celery worker (rev 11).

Invoked by ECS via task-def healthCheck.command. Exit code semantics:
    0  -> heartbeat file mtime is fresh (worker process is alive)
    1  -> heartbeat file is missing OR stale OR unreadable

Strategy (rev 11) -- file-mtime heartbeat:
    The worker process touches HEARTBEAT_PATH every ~15s from a daemon
    thread started in app/worker/celery_app.py. Each touch also emits
    a log line ("healthcheck heartbeat: touched ...") visible in
    CloudWatch. The probe simply stat()s the file and compares mtime
    to wall clock.

    Freshness window: HEARTBEAT_FRESHNESS_SECONDS = 60. The producer
    fires every 15s, so the probe accepts up to 4 consecutive missed
    heartbeats before failing. This is robust against transient
    scheduler hiccups while still catching a truly wedged worker.

History of why we ended up here (rev 7..10 all failed in production):
    Rev 7  - `celery -A ... inspect ping -d celery@$HOSTNAME`
             Failed: $HOSTNAME on Fargate did not match Celery's
             socket.getfqdn()-derived node name.
    Rev 8  - `celery -A ... inspect ping` (no -d flag)
             Failed: requires a broker control-channel round-trip via
             SQS, which is unreliable with --without-mingle/--gossip.
             Also: HEALTHCHECK CMD-SHELL stdout/stderr is captured in
             Docker's per-container health buffer, NOT awslogs, so we
             could not observe the failure mode from CloudWatch.
    Rev 9  - Python /proc walk, argv[0] basename == 'celery'
             Failed: pip-installed `celery` is an entry-point script
             that gets exec'd via the Python interpreter. argv[0] in
             /proc/<pid>/cmdline is /usr/local/bin/python3.14, with
             'celery' at argv[1]. Local test was wrong (used direct
             exec of a renamed binary).
    Rev 10 - Python /proc walk, b'celery' AND b'worker' in argv elements
             Failed: still unobservable from CloudWatch. Probably
             logically correct but we could not verify, and the
             circuit breaker rolled it back after the same ~129s
             healthcheck-timeout pattern as rev 8/9.

Rev 11's key advantage: the PRODUCER side logs are observable in
CloudWatch. If CloudWatch shows ~15s heartbeat log lines, the producer
is healthy and any probe failure is a probe-side bug. If logs show no
heartbeats, the worker is wedged (or never started the heartbeat
thread) and the probe correctly reports unhealthy.

What this probe deliberately does NOT detect:
    - Hung consumer event loop (heartbeat thread is independent)
    - Lost broker connection (cascades to process exit within ~10s,
      at which point heartbeat thread dies with process)
    These are mitigated by task_acks_late=True + 30s SQS visibility
    timeout: messages redelivered to other workers automatically.

Implementation:
    Pure stdlib. Reads one file's mtime. Sub-millisecond.
    Self-isolated from any Celery/kombu/boto import surface.
"""
from __future__ import annotations

import os
import sys
import time

HEARTBEAT_PATH = "/tmp/celery_alive"
HEARTBEAT_FRESHNESS_SECONDS = 60


def heartbeat_is_fresh() -> bool:
    """Return True iff HEARTBEAT_PATH exists and was modified recently."""
    try:
        st = os.stat(HEARTBEAT_PATH)
    except FileNotFoundError:
        # Worker has not started the heartbeat thread yet (still inside
        # startPeriod) OR worker crashed before worker_ready fired.
        return False
    except OSError:
        # /tmp unreadable -- treat as unhealthy
        return False
    age = time.time() - st.st_mtime
    return age <= HEARTBEAT_FRESHNESS_SECONDS


def main() -> int:
    return 0 if heartbeat_is_fresh() else 1


if __name__ == "__main__":
    sys.exit(main())
