# Step 28 Phase 2 Commit 7 — Container HEALTHCHECK rev 11

**Date:** 2026-05-05 (afternoon session, ~14:30–16:15 EDT)
**Branch:** `step-28-hardening-impl`
**Final state:** Commit 7 SHIPPED. Worker production task-def `luciel-worker:11`
serving traffic with HEALTHCHECK `HEALTHY`, container HEALTHCHECK reporting
`HEALTHY` from a producer-side file-mtime heartbeat strategy. Four prior
revisions (rev 7, 8, 9, 10) all failed in production for distinct reasons;
rev 11 was the one that crossed the finish line. Total cycles: 5 image
builds, 5 ECR pushes, 5 task-def registrations, 5 service-update rollouts,
4 circuit-breaker rollbacks to rev 6, 1 successful cutover.

This recap exists because the journey from "add a healthcheck" to "ship a
working healthcheck" was meaningfully educational about Fargate's
container-runtime semantics, Celery's exec model, and — most importantly —
the **observability gap between HEALTHCHECK CMD-SHELL output and
CloudWatch awslogs**. That gap drove four consecutive failures that all
looked the same (silent rollback, no probe-side signal in CloudWatch),
and breaking out of it required inverting the entire probe topology.

---

## TL;DR

| Rev | Probe strategy | Failure mode | Commit |
|---|---|---|---|
| 7 | `celery inspect ping -d celery@$HOSTNAME` | `$HOSTNAME` ≠ Celery's `socket.getfqdn()` node name on Fargate | `837da98` |
| 8 | `celery inspect ping` (no -d flag) | broker round-trip with `--without-mingle/--without-gossip` unreliable; AND probe stdout structurally not in awslogs | `27723b0` |
| 9 | Python /proc walk, `argv[0]` basename `== 'celery'` | pip entry-point scripts exec via Python interpreter; `argv[0]` is `/usr/local/bin/python3.14`, not `celery` | `594821e`, `bb6dd7a` |
| 10 | Python /proc walk, element-membership match `b'celery' in argv AND b'worker' in argv` | local mocks now matched production exec pattern; logic was probably correct but probe still failed in production. Diagnosis blocked by the same CMD-SHELL → awslogs gap. | `d56f08c`, `dbdc469` |
| **11** | **Producer-side**: worker daemon thread touches `/tmp/celery_alive` every 15s + logs to CloudWatch. **Probe-side**: stat the file, accept if mtime within 60s. | None — `HEALTHY` after first probe cycle, rollout `COMPLETED`, deployment cutover at 16:10–16:14 EDT. | `079f327`, `fceb7e9` |

---

## The structural lesson

**Container HEALTHCHECK CMD-SHELL stdout/stderr is captured in Docker's
per-container health buffer, NOT awslogs.** This is a Docker daemon
property that propagates through Fargate's containerization layer. When a
HEALTHCHECK probe fails, its output is reachable only via
`docker inspect <container> --format '{{json .State.Health}}'` — which
on Fargate is not reachable from the operator's laptop because there's
no SSH/ECS-Exec into the task during a failed-probe lifecycle window
(the task gets killed before exec is practical).

This means **a HEALTHCHECK probe is structurally unobservable from
CloudWatch**. Probes that fail silently and roll back via the deployment
circuit breaker generate the same operator-side signal regardless of why
they failed: a 5-minute wait, a `describe-services` poll showing the
service back on the previous revision, no probe output anywhere.

Rev 7→10 each implemented a different probe strategy and each failed for
a different reason, but the failure *mode* was identical: silent rollback,
zero diagnostic signal. We could only reason about why each one might
have failed, not observe it.

Rev 11 inverts the producer/consumer relationship of the healthcheck:
- **The producer is the worker process itself**, which actively logs
  `healthcheck heartbeat: touched /tmp/celery_alive` via the Python
  logger every 15s. Worker logs go to awslogs. The producer is fully
  observable.
- **The probe is the consumer**: a 4-line Python script that stats one
  file. It contains no broker call, no Celery import, no /proc walk —
  the smallest probe surface area we can construct.

The tradeoff: the probe still produces unobservable output (any failure
of the stat call is invisible). But because the producer is observable,
we can split any future failure cleanly:
- CloudWatch shows heartbeat lines AND ECS reports unhealthy → probe-side
  bug. We control the script, fix in 5 min.
- CloudWatch shows no heartbeat lines after `worker_ready` → worker is
  truly wedged or never reached `worker_ready`. ECS is correctly reporting
  unhealthy. Investigate the worker, not the probe.
- CloudWatch shows heartbeat lines, then they stop → real wedge caught
  in real time. System worked as designed.

This is the diagnostic-bisection property the rev 7→10 family didn't
have, and it's why rev 11 was the right strategy independent of
implementation.

---

## Detailed timeline

### Rev 7 (failure 1) — `celery inspect ping -d celery@$HOSTNAME`

**Hypothesis:** Use Celery's own RPC `inspect ping` to confirm the worker
is alive and broker-connected. The `-d` flag scopes the ping to a specific
node name — we used `celery@$HOSTNAME` because that's the documented
Celery node-name pattern.

**Failure:** On Fargate, `$HOSTNAME` resolves to something like
`ip-10-0-11-84` (the ENI's internal IP-derived hostname). Celery
constructs its node name from `socket.getfqdn()` which on Fargate
returned `ip-10-0-11-84.ca-central-1.compute.internal`. Mismatch:
the probe pinged `celery@ip-10-0-11-84` but the actual node was
`celery@ip-10-0-11-84.ca-central-1.compute.internal`. Probe got no
response, exit 1, container marked unhealthy.

**Drift surfaced:** `D-celery-fargate-hostname-mismatch-in-healthcheck-2026-05-05`

### Rev 8 (failure 2) — `celery inspect ping` (no -d flag)

**Hypothesis:** Drop the `-d` flag. Celery's inspect-ping without a
target should round-robin or broadcast to all workers, getting a
response from any of them. Simpler, no hostname-coupling.

**Failure:** This required a working broker control-channel round-trip.
Our worker runs with `--without-mingle --without-gossip --without-heartbeat`
(deliberate config, see `app/worker/celery_app.py` — these reduce noise on
SQS broker and avoid PID/hostname-coupled control messages). With those
flags, the control-channel reply path is unreliable — the worker
processes the inspect request but the reply often doesn't make it back
to the inspecting CLI before the timeout.

**Crucially:** even if we'd diagnosed this from logs, we couldn't —
HEALTHCHECK CMD-SHELL output never made it to awslogs. We were guessing
based on first principles about why rev 8 failed, not observing it.

**Drift surfaced:** `D-celery-inspect-ping-unobservable-on-fargate-2026-05-05`
(the more important drift was identified here, not at rev 7).

### Rev 9 (failure 3) — Python /proc walk, argv[0] basename check

**Hypothesis:** Stop relying on Celery RPC entirely. Walk `/proc` in
Python, look at every PID's `cmdline`, and pass if any process has
`argv[0]`'s basename equal to `b'celery'`.

**Files added:** `scripts/healthcheck_worker.py` (Python /proc walker),
Dockerfile updated to install `procps`. ECR rebuild + new image digest
`sha256:47a073f1...d04bfb`.

**Failure:** The `celery` command in our pip-installed image is a
**Python entry-point script** at `/usr/local/bin/celery`. When the kernel
exec's it, the shebang line redirects through the Python interpreter, so
`/proc/<pid>/cmdline` shows:
```
argv[0] = /usr/local/bin/python3.14
argv[1] = /usr/local/bin/celery
argv[2..] = -A app.worker.celery_app worker --loglevel=info ...
```
Our check `os.path.basename(argv[0]) == b'celery'` was looking for
`celery` at index 0, but `celery` is actually at index 1.

**Local test was wrong** — I used a direct exec of `/tmp/celery` (a
copy of the python binary renamed), which made `argv[0]` basename
literally `'celery'`. That doesn't match production's pip-entry-point
exec pattern. The test passed locally and shipped a broken probe.

**Drift surfaced:** `D-pip-entrypoint-argv0-is-python-not-script-name-2026-05-05`

### Rev 10 (failure 4) — Element-membership match in argv

**Hypothesis:** Fix rev 9's argv[0] mistake. Split cmdline on NUL
into argv elements, require `b'celery' in argv` AND `b'worker' in argv`
as exact element-membership checks. This handles both the pip
entry-point shape (where `celery` is at argv[1]) and any other shape
the kernel might produce. Element membership rejects substring-only
matches (e.g. the script's own filename `/app/scripts/healthcheck_worker.py`
contains the bytes `worker` but not as a bare argv element).

**Files modified:** `scripts/healthcheck_worker.py`. ECR rebuild + new
image digest `sha256:b458a824...266635d`.

**Local tests** — three cases all passed with mocks that *correctly*
mimicked the production exec pattern this time (Case B used
`python3 -c "..." celery worker` so `argv[0] = python3` and `celery`/`worker`
appeared as bare argv elements at indices ≥1, exactly the production
shape).

**Failure:** The probe still failed in production. Logic was probably
correct, but we couldn't prove it because — same gap as rev 8 — the
probe's stdout was unreachable from CloudWatch. After 41 minutes
(circuit-breaker retry budget exhausted), ECS rolled back to rev 6.
By the time we tried to read the failed task's logs, ECS had already
evicted the stopped tasks (Fargate's stopped-task retention is short).

This was the failure that forced the strategic shift: we were burning
diagnosis cycles on a problem we could not observe. Stopping to add
observability had higher leverage than another probe-logic iteration.

**Drift surfaced:** `D-healthcheck-cmdshell-output-not-in-awslogs-2026-05-05`
(this had been latent since rev 8 but became the explicit blocker here).

### Rev 11 (success) — Producer-side heartbeat + mtime probe

**Strategic shift (logged via `ask_user_question` at 15:57 EDT):**
options were (a) concede Commit 7 and accept rev 6 no-healthcheck baseline,
(b) one more diagnosis attempt by manually launching rev 10 via
`run-task` to read its boot logs, (c) try rev 11 with file-touch
heartbeat. User chose option C — "we are designing a business and we
cant afford any compromises in the long run."

**Design:**

1. **Producer (in `app/worker/celery_app.py`):** Hooked Celery's
   `worker_ready` signal. On signal fire, immediately touch
   `/tmp/celery_alive` and start a daemon thread that:
   - Touches `/tmp/celery_alive` every 15s (atomic via
     `with open("a"): os.utime(path, None)` — `pathlib.Path.touch`
     equivalent, but stdlib-only)
   - Logs `healthcheck heartbeat: touched /tmp/celery_alive` at INFO
     (via the same logger that the worker uses for everything else, so
     it goes to awslogs)
   - Catches OSError on the touch, logs at ERROR, continues — never
     crashes the worker on probe-related I/O failure
   - Uses `threading.Event.wait(15)` for the sleep so shutdown is
     responsive (no need to wait full interval before noticing
     `worker_shutdown` was signaled)
   - Hooked `worker_shutdown` to set the stop event and let the daemon
     thread exit cleanly during ECS task drain

2. **Probe (in `scripts/healthcheck_worker.py`):** Replaced the /proc
   walker with a 4-line stat check. `os.stat('/tmp/celery_alive')`,
   compare `time.time() - st.st_mtime` against 60s freshness window.
   Exit 0 if fresh, 1 if missing or stale. No imports beyond os/sys/time.

3. **Liveness semantics this captures:** "The worker process Python
   interpreter is alive and scheduling daemon threads." This is the
   same liveness semantics as rev 9/10's process-existence check, plus
   the requirement that the threading scheduler is making forward
   progress. If the GIL is wedged or the entire interpreter has hung,
   the heartbeat thread can't tick.

4. **What it deliberately does NOT detect:**
   - Hung consumer event loop (heartbeat thread is independent)
   - Lost broker connection (cascades to process exit within ~10s
     via kombu's error handler; at process exit, heartbeat thread dies
     with the process, file goes stale, probe correctly reports
     unhealthy)
   - These are mitigated by `task_acks_late=True` + 30s SQS visibility
     timeout — in-flight messages get redelivered automatically. No
     data loss on hung-worker scenarios.

**Local verification (4 cases, all passed):**
- A: heartbeat file missing → probe exits 1
- B: fresh mtime within window → probe exits 0
- C: stale mtime older than window → probe exits 1
- D: ASCII cleanliness of probe + 87 lines of producer-side additions

**End-to-end smoke (host Python with 1s interval for fast test):**
- File created on `_start_heartbeat()` ✓
- mtime advances at configured cadence ✓
- 3 INFO log lines emitted, format matches expected CloudWatch shape ✓
- `_stop_heartbeat()` halts the loop, mtime stops advancing ✓

**Production rollout (16:10–16:14 EDT):**
- ECR push: `worker-rev11` digest `sha256:f5ae6997...763f3da0`
- Task-def: `luciel-worker:11` registered ACTIVE
- `update-service` to `luciel-worker:11` at 16:10 EDT
- Task `6a7be8840a184b44bf89879c67e1d886` placed at 16:10:28, started
  at 16:10:49 (21s placement → start)
- `worker_ready` fired at 16:10:55 (6s after task start)
- First heartbeat log line at 16:10:55.521
- 17 heartbeat events observed in CloudWatch over the next 3.5 min, at
  exactly 15.000s ± 1ms cadence
- ECS marked container `HEALTHY` after first probe cycle
- Rev 6 deployment drained
- Service `describe-services` at 16:14 EDT showed: single PRIMARY
  deployment on rev 11, `rolloutState: COMPLETED`, `failedTasks: 0`

---

## Code artifacts shipped

```
079f327  p3-s-half-2(healthcheck-rev11): file-mtime heartbeat with observable producer
  app/worker/celery_app.py           +87 lines  (producer)
  scripts/healthcheck_worker.py      replaced   (probe — 90 lines)
  Dockerfile                         comment-only update

fceb7e9  p3-s-half-2(td-rev11): bind worker task-def to rev11 image digest
  worker-td-rev11.json               new file
```

ECR image digest: `sha256:f5ae6997cf2a9f3b75a1488994810f61054c8fbf1299a2e106be8558763f3da0`
ECR tag: `worker-rev11`

Prior revisions (preserved in repo for audit trail / rollback):
```
837da98  worker-td-rev7.json
27723b0  worker-td-rev8.json
594821e  scripts/healthcheck_worker.py (rev 9 initial /proc walker) + Dockerfile procps
bb6dd7a  worker-td-rev9.json
d56f08c  scripts/healthcheck_worker.py (rev 10 element-membership)
dbdc469  worker-td-rev10.json
```

---

## Drifts surfaced and resolved this session

All resolved in-session by the rev 11 strategy + commits above.

1. **D-celery-fargate-hostname-mismatch-in-healthcheck-2026-05-05** —
   `$HOSTNAME` ≠ Celery's `socket.getfqdn()` node name on Fargate.
   Resolved: rev 8 dropped the `-d` flag, then rev 11 abandoned
   celery-inspect entirely.

2. **D-celery-inspect-ping-unobservable-on-fargate-2026-05-05** —
   `celery inspect ping` requires a broker control-channel round-trip
   that's unreliable with `--without-mingle/--without-gossip`, AND its
   output goes to Docker's per-container health buffer, not awslogs.
   Resolved: rev 11 abandoned the inspect approach entirely; the new
   probe makes no broker calls and the producer side is observable in
   awslogs.

3. **D-healthcheck-cmdshell-output-not-in-awslogs-2026-05-05** —
   Container HEALTHCHECK CMD-SHELL stdout/stderr is captured in
   Docker's per-container health buffer, not awslogs. Probe failures
   are structurally invisible from CloudWatch. Resolved at the
   strategic level: rev 11 puts the OBSERVABLE signal on the producer
   side (worker logs) rather than the probe side. The probe still has
   unobservable output, but that no longer matters because the
   producer log lines tell us authoritatively whether liveness is
   being reported.

4. **D-pip-entrypoint-argv0-is-python-not-script-name-2026-05-05** —
   Pip-installed Python entry-point scripts get exec'd through the
   Python interpreter, so `/proc/<pid>/cmdline argv[0]` is the python
   binary path, not the script name. Resolved at rev 10 via
   element-membership match. Kept as a forward-looking guard for any
   future Python-CLI-based diagnostic tooling.

5. **D-ecs-service-name-asymmetry-with-td-family-2026-05-05** —
   The service is `luciel-worker-service`, but the task-def family is
   `luciel-worker`. This bit early in the session when commands
   targeted the wrong name. Resolved: documented the asymmetry in this
   recap and in the runbook §7 update; service name vs TD family
   distinction now explicit in every relevant `aws ecs` command.

6. **D-operator-pull-skipped-before-write-side-aws-ops-2026-05-05** —
   Twice in the session, advisor authored a task-def, committed and
   pushed, then immediately handed an `aws ecs register-task-definition
   --cli-input-json file://...` command to operator without first
   instructing them to `git pull`. Resolved by adding "operator must
   `git pull origin step-28-hardening-impl` before any AWS write-side
   call referencing local `file://` JSON" as a non-negotiable workflow
   invariant for the rest of the session, and codifying it here as a
   forward-looking guard.

---

## Forward-looking guards (lessons that outlive this session)

1. **Container HEALTHCHECK probes that need to be debuggable should
   either: (a) use producer-side observability (rev 11 pattern), or
   (b) explicitly pipe their stdout to a known awslogs-visible
   channel.** A "side B" approach for future ALB-target-group probes
   could be `python -c "..." > /proc/1/fd/1 2>&1` to send probe output
   to PID 1's stdout, which IS captured by awslogs — but rev 11's
   pattern is cleaner and we should default to it.

2. **Local mocks for /proc-based probes must match the production exec
   pattern**, not just produce a binary at the right path. Rev 9's
   bug shipped because the local mock used direct binary exec while
   production uses pip-entry-point Python interpreter exec. Future
   probe testing should explicitly construct argv shapes that
   reproduce the production cmdline byte-for-byte.

3. **Service name vs task-def family asymmetry is real and must be
   verified up-front** for any service touched by ECS automation. Use
   `describe-services` first, never assume `service-name == td-family`.

4. **After advisor pushes any commit referenced by an upcoming `file://`
   AWS CLI invocation, operator MUST `git pull` before running the
   command.** This is now invariant for the rest of the engagement.

5. **Producer-side observable signals beat unobservable probes for
   anything that needs to survive a 2am incident review.** When in
   doubt about diagnostic strategy: invert the probe so the actor
   you're checking does the logging itself, then alarm on absence of
   the log line.

---

## What this unblocks

- Commit 7 ✅ DEPLOYED HEALTHY (this commit)
- Commit 5 (CloudWatch alarms) — can now include a high-leverage alarm:
  **MetricFilter on `/ecs/luciel-worker` log group for the heartbeat
  pattern**, alarm fires if < 1 occurrence in 90s window over 2
  consecutive periods. This is the earliest possible signal of worker
  wedge, fires before ECS even marks the container unhealthy.
- Commit 6 (autoscaling) — unaffected by Commit 7's evolution.
- Phase 2 close gate — no longer blocked by "worker container has no
  HEALTHCHECK." Single remaining items are Commit 5 + Commit 6.

---

## Sources

Per-commit messages on `step-28-hardening-impl` branch (chronological):
- `837da98` — rev 7 td: add HEALTHCHECK with celery@$HOSTNAME probe
- `27723b0` — rev 8 td: drop -d flag (hostname mismatch)
- `594821e` — rev 9 infra: add /proc walker script + procps in image
- `bb6dd7a` — rev 9 td: bind to procps image + Python /proc probe
- `d56f08c` — rev 10 fix: element-membership match in argv
- `dbdc469` — rev 10 td: bind to corrected probe image
- `079f327` — rev 11: file-mtime heartbeat with observable producer
- `fceb7e9` — rev 11 td: bind to rev 11 image digest

CloudWatch evidence preserved in session record:
- Rev 7-10 failure pattern: 129s ± 1s healthcheck-timeout window, clean
  Celery boot to "ready" before each kill, ZERO probe-side output in
  log streams.
- Rev 11 success: 17 heartbeat log events at 15.000s ± 1ms cadence
  starting at 16:10:55.521 EDT, container reaching HEALTHY, deployment
  cutover at 16:14 EDT.
