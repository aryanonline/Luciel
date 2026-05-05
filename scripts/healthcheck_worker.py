#!/usr/bin/env python3
"""
Container HEALTHCHECK probe for the Luciel Celery worker.

Invoked by ECS via task-def healthCheck.command. Exit code semantics:
    0  -> at least one Celery worker process is running in this PID namespace
    1  -> no Celery worker process found (or /proc unreadable)

Why process-liveness instead of `celery inspect ping`:
    The semantically richer probe (`celery -A ... inspect ping`) requires
    a working Celery control-channel round-trip via the broker. With our
    SQS broker + --without-mingle/--without-gossip worker config, ping
    consistently returned non-zero in production (see rollout failures of
    luciel-worker:7 and :8 in the May 5 2026 deployment runbook). The
    HEALTHCHECK output is captured in Docker's per-container health buffer,
    not in awslogs, so we cannot observe its failure mode in CloudWatch.

    Process-liveness is structurally observable, has zero external
    dependencies, and detects the failure modes that actually matter in
    production for our workload:
        - Crashed worker (process gone)
        - OOM-killed worker (process gone)
        - Container started with wrong CMD (no celery process)

    What it deliberately does NOT detect:
        - Hung worker (process exists but wedged)
        - Lost broker connection (process exists, can't pull jobs)

    These are mitigated by:
        - task_acks_late=True + 30s SQS visibility timeout: messages are
          redelivered to other workers (or back to a restarted worker)
          automatically. No data loss on hung-worker scenarios.
        - Real broker failures cascade into worker process exits within
          seconds because kombu raises and Celery's error handler
          eventually exits, at which point process-liveness catches it.

Match criterion (STRICT to avoid self-matching):
    The probe MUST NOT match itself or any other process that incidentally
    has the bytes 'celery' and 'worker' in its cmdline (e.g. a Python script
    whose path or docstring mentions those words). Substring matching is
    UNSAFE: the healthcheck process running this script has a cmdline like
    'python3 /app/scripts/healthcheck_worker.py', which contains 'worker'
    in the script filename. Substring matching would create a false-positive
    where the probe ALWAYS passes because it counts itself.

    Correct criterion: parse cmdline as NUL-separated argv, then require:
        1. argv[0] basename is exactly 'celery' (the binary name, not
           a path that contains 'celery' somewhere)
        2. AND 'worker' is one of the argv elements (Celery subcommand)

    Our actual worker command:
        celery -A app.worker.celery_app worker --loglevel=info ...
    argv[0] = 'celery'             -> basename match
    argv[3] = 'worker'             -> subcommand match
    Celery prefork creates 1 main process + N child workers (N=concurrency=2).
    All N+1 processes share the same cmdline, so any of them satisfies the
    probe. We need >=1, not exactly N+1.

Implementation notes:
    - Pure stdlib; no celery, no kombu, no boto3 imports. Probe latency
      is sub-millisecond.
    - Reads /proc/<pid>/cmdline as bytes (cmdline is NUL-separated raw
      bytes; decoding to str can fail on some kernels for kernel threads).
    - Defensive: silently skips PIDs that disappear between listdir and
      open (race with process exits).
    - Skips empty cmdlines (kernel threads have empty /proc/<pid>/cmdline).
    - Uses os.path.basename on argv[0] so 'celery', './celery', and
      '/usr/local/bin/celery' all match correctly.
"""
from __future__ import annotations

import os
import sys


def worker_process_exists() -> bool:
    """Return True if any process in this PID namespace is a Celery worker.

    Strict argv-based match:
        argv[0] basename == 'celery' AND 'worker' is in argv
    See module docstring for why substring matching is unsafe.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        # /proc not mounted -- should never happen in a container, but
        # if it does, we have bigger problems than the healthcheck
        return False

    for entry in entries:
        if not entry.isdigit():
            continue
        cmdline_path = f"/proc/{entry}/cmdline"
        try:
            with open(cmdline_path, "rb") as f:
                cmdline = f.read()
        except (OSError, FileNotFoundError):
            # Process exited between listdir and open; skip
            continue
        if not cmdline:
            # Kernel threads have empty cmdline; skip
            continue
        # cmdline is NUL-separated argv. Split into argv elements.
        # There's a trailing NUL after the last arg, which produces an
        # empty string at the end; filter it.
        argv = [a for a in cmdline.split(b"\x00") if a]
        if not argv:
            continue
        # argv[0] basename match -- handles 'celery', './celery',
        # '/usr/local/bin/celery' uniformly
        argv0_basename = os.path.basename(argv[0])
        if argv0_basename != b"celery":
            continue
        # 'worker' subcommand must be in argv (any position)
        if b"worker" not in argv:
            continue
        return True
    return False


def main() -> int:
    return 0 if worker_process_exists() else 1


if __name__ == "__main__":
    sys.exit(main())
