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

Match criterion (STRICT to avoid self-matching, REVISED in rev 10):
    The probe MUST NOT match itself or any other process that incidentally
    has the bytes 'celery' and 'worker' in its cmdline (e.g. a Python script
    whose path or docstring mentions those words). Substring matching is
    UNSAFE: the healthcheck process running this script has a cmdline like
    'python3 /app/scripts/healthcheck_worker.py', which contains 'worker'
    in the script filename. Substring matching would create a false-positive
    where the probe ALWAYS passes because it counts itself.

    REVISED criterion (rev 10, after rev 9's argv[0] check failed in prod):
    Parse cmdline as NUL-separated argv, then require BOTH 'celery' AND
    'worker' to be EXACT elements (not substrings) of argv.

    Why argv[0] basename check was wrong in rev 9:
        The 'celery' command in our pip-installed image is a Python entry-
        point script at /usr/local/bin/celery. When the kernel exec's it,
        the shebang line redirects through python, so /proc/<pid>/cmdline
        shows argv[0] = /usr/local/bin/python3.14 (or similar), with
        'celery' appearing as argv[1]. Rev 9's check (argv[0] basename ==
        'celery') failed every time, exiting 1, marking the container
        unhealthy. Verified by reasoning about Python entry-point semantics
        and the fact that the worker boot logs showed ready state but the
        healthcheck never passed in 129s of probe attempts.

    Our actual worker command at the task-def level:
        celery -A app.worker.celery_app worker --loglevel=info ...
    But /proc/<celery-worker-pid>/cmdline shows:
        /usr/local/bin/python3.14 /usr/local/bin/celery -A app.worker.celery_app worker ...
    So both 'celery' and 'worker' appear as DISTINCT argv elements after
    NUL-splitting. The element-membership check (b'celery' in argv) will
    match these but will NOT match a process whose argv contains a path
    like '/app/scripts/healthcheck_worker.py' (which is one element
    containing 'worker' as a substring, but the element itself is not
    equal to b'worker').

    Celery prefork creates 1 main process + N child workers (N=concurrency=2).
    All N+1 processes share similar cmdlines, so any of them satisfies the
    probe. We need >=1, not exactly N+1.

Implementation notes:
    - Pure stdlib; no celery, no kombu, no boto3 imports. Probe latency
      is sub-millisecond.
    - Reads /proc/<pid>/cmdline as bytes (cmdline is NUL-separated raw
      bytes; decoding to str can fail on some kernels for kernel threads).
    - Defensive: silently skips PIDs that disappear between listdir and
      open (race with process exits).
    - Skips empty cmdlines (kernel threads have empty /proc/<pid>/cmdline).
    - Uses element-equality on the full argv list, NOT substring or
      basename matching, to avoid false positives from script paths
      that contain 'celery' or 'worker' as path components.
"""
from __future__ import annotations

import os
import sys


def worker_process_exists() -> bool:
    """Return True if any process in this PID namespace is a Celery worker.

    Element-membership match (rev 10):
        b'celery' in argv  AND  b'worker' in argv
    Where argv is /proc/<pid>/cmdline split on NUL bytes.

    Element membership (not substring) is required to avoid the
    self-matching false positive where the probe's own cmdline
    (python3 /app/scripts/healthcheck_worker.py) substring-matches
    'worker' via the script filename. Element membership requires
    an EXACT byte-equal match on at least one full argv element.

    See module docstring for why argv[0] basename check (rev 9) failed
    in production despite passing local tests.
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
        # Both 'celery' and 'worker' must appear as EXACT elements of argv.
        # NOT substring match: '/app/scripts/healthcheck_worker.py' contains
        # 'worker' as a substring but is not equal to b'worker'.
        # NOT argv[0]-only check: pip entry-point scripts get exec'd with
        # argv[0] = python interpreter and the script path at argv[1+].
        if b"celery" not in argv:
            continue
        if b"worker" not in argv:
            continue
        return True
    return False


def main() -> int:
    return 0 if worker_process_exists() else 1


if __name__ == "__main__":
    sys.exit(main())
