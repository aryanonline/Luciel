# 2026-05-04 — Pillar 13 A3 real root cause + Commit A/D session recap

**Branch:** `step-28-hardening-impl`
**Commits this session:**
- `da3d545` — `diag(28-p13)`: P13_DIAG instrumentation
- `81b9e5a` — `fix(28-p13-a)`: bind `actor_user_id` from `agent.user_id` in auth middleware (the fix)
- `13035da` — `chore(28-p13)`: remove P13_DIAG; archive 19/19 verification report
- `7e2dab1` parent (P3-H docs)

**Drift entry resolved:** `D-pillar-13-a3-real-root-cause-2026-05-04`

**Verification report archived at:** `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json`

---

## 1. The lie this fix removes from the product

Before Commit A, the chat turn flow on the legitimate Pillar 13 A3
setup turn produced this customer-facing sequence:

1. User sends a memory-eligible message ("My favorite city is Toronto").
2. Backend returns 200 with assistant reply containing "I'll remember
   that for next time."
3. **Zero `MemoryItem` rows are written.**
4. The next session's recall has no record of the fact.

The product told the customer it remembered something. It did not.
For an AI brokerage assistant whose entire pricing model rests on
*scope-correct memory* this is the worst possible class of bug —
silent integrity loss that contradicts a marketing claim. Per the
user's standing instruction: *"we cannot make any compromises in our
security and programmatic errors. If we become lazy this could lead
us to getting sued."*

This recap documents how the bug was found, what the real root cause
turned out to be, and why three intermediate hypotheses were wrong.

---

## 2. Failure chain (now confirmed)

```
ApiKeyAuthMiddleware.dispatch  (app/middleware/auth.py:124, pre-Commit-A)
    user_id = agent.user_id          # ← TYPO: never read again
    request.state.actor_user_id = actor_user_id   # ← stays None

ChatService._handle_chat_turn  (app/services/chat_service.py)
    actor_user_id = getattr(request.state, "actor_user_id", None)  # → None
    memory_service.extract_and_save(..., actor_user_id=None)        # passed through

MemoryService.extract_and_save  (sync path)
    INSERT INTO memory_items (..., actor_user_id=NULL, ...)

Postgres
    ERROR: null value in column "actor_user_id" violates not-null constraint
    (D11 constraint, intentional and verified by Pillar 16)

extract_and_save:116-119
    except Exception:
        logger.warning("memory extraction failed: %s", type(exc).__name__)
        # IntegrityError swallowed; no repr; chat turn returns 200
```

**Three independent design decisions had to align for this to be
silent:**

1. The `except Exception` in `extract_and_save` logs only `type(exc).__name__`,
   not `repr(exc)`. The IntegrityError's actual message ("null value in
   column actor_user_id") never surfaced.
2. The auth middleware's local-variable typo bound `user_id` (a
   never-read local) instead of `actor_user_id` (the request-state
   binding).
3. The chat turn's fail-open contract on memory extraction is
   correct in principle (a down memory pipeline must not break the
   chat turn) but in practice it converted a deterministic schema
   violation into a customer-facing lie.

The fix is one line in (2). The compounding factors in (1) and (3)
are tracked as separate Phase-3 hygiene items (see §6).

---

## 3. The three hypotheses I had to discard

Honest documentation requires preserving wrong turns. The user
called this out explicitly: *"I hope we are making honest long term
fixes and not just takin shortcuts."*

### Hypothesis 1 (WRONG): "the message text is not extractable"

**Commit `07fd3c0`** rewrote the A3 setup-turn message from a
prose+tool-call shape to a clean user-fact shape, on the theory that
the extractor was rejecting the LLM reply as non-extractable. This
reasoning was based on inspecting the JSON-parse path in the
extractor and noticing it required a tight schema.

This was reverted by `23f228e` after the P13_DIAG instrumentation in
`da3d545` proved that the extractor *did* produce a valid memory
candidate when fed the original prose+tool-call style. The bug was
never in the extractor; it was upstream.

### Hypothesis 2 (WRONG): "extractor JSON parse needs a tolerant fallback"

The "Commit B (B-hybrid)" plan was going to: tighten the prompt,
add a tolerant JSON parser fallback, log `repr(exc)` on save fail,
and add a drift audit row. After the second repro post-Commit-A
showed `MemoryItem id=223` written cleanly with the same prose+tool-
call style reply, this entire commit was withdrawn. The system was
always architected correctly for this input shape; only the auth
binding was broken.

### Hypothesis 3 (DOWNGRADED): "production async default mismatch"

The "Commit C" plan was going to flip
`settings.memory_extraction_async` to `True` for prod parity.
Pillar 11 (async memory extraction) passed in the post-Commit-A
verification, which means the async path also works end-to-end.
The flag mismatch is a config-hygiene item, not a correctness fix.
Logged for future config consolidation but no longer Phase-2-blocking.

---

## 4. Sequence (chronological)

| Step | Window / Action | Outcome |
|---|---|---|
| 1 | Commit `da3d545` (P13_DIAG diag) pushed | logging in place |
| 2 | Operator pulls + restarts uvicorn (PID 60552) + worker | services up |
| 3 | First `diag_p13_repro.py` run | 0 MemoryItem rows; P13_DIAG log: `actor_user_id=None`, `sync-path-taken` |
| 4 | Read `auth.py:124` | typo `user_id = agent.user_id` confirmed |
| 5 | Commit A built in agent sandbox (one-line fix + 12-line forensic comment + 5-test regression guard) | tests two-way verified (FAIL with bug, PASS with fix) |
| 6 | Commit `81b9e5a` pushed | regression guard active |
| 7 | Operator pulls + runs 5/5 PASS + restarts uvicorn (PID 61616) | services up |
| 8 | Second `diag_p13_repro.py` run | `MemoryItem id=223` written with sentinel `DIAG-LEGIT-0CF522`, valid `actor_user_id` |
| 9 | Full Pillar verification | **19/19 GREEN** including P11 (async), P13 (spoof+legit), P16 (D11) |
| 10 | Threat model audit (5 threats, 0 new attack surface) | no behavior shifts beyond binding fix |
| 11 | grep audit: only consumer of `request.state.actor_user_id` is `chat.py:40,72` | both pass-through, no conditional branching |
| 12 | Commit D built (this commit): strip P13_DIAG from `auth.py` + `chat_service.py`, delete `diag_p13_repro.py`, archive verification report | clean repo |

---

## 5. Forensic regression guard

Five tests at `tests/middleware/test_actor_user_id_binding.py`
prevent this exact regression class:

- **Test 0 (AST canary):** asserts the source line `actor_user_id = agent.user_id`
  exists in `auth.py`. Catches anyone reverting the fix textually.
- **Tests 1–4 (behavioral):** mock `AgentRepository`, drive the
  middleware end-to-end, assert `request.state.actor_user_id`
  equals `agent.user_id` for various agent/key shapes.

The two-way proof — failing with the original typo, passing with the
fix — is captured in the commit message of `81b9e5a` and reproducible
by a `git revert` followed by `pytest tests/middleware/`.

---

## 6. Compounding factors logged as Phase-3 items

These are hygiene gaps the bug exposed but did not cause:

- **P3-O — extractor failure observability** (NEW). `extract_and_save:116-119`
  swallows `IntegrityError` (and any save-time exception) with a
  `type-only` warning. Recommend `logger.warning("... %r", exc)` plus
  a drift audit row for save-time exceptions. Without this, future
  schema-constraint violations will again be silent.
- **P3-N — preflight ritual silently runs degraded with no Celery worker**
  (NEW). The 5-block pre-flight passes when Celery is down because the
  sync fallback path takes over. Recommend pre-flight gate fails fast
  if `celery -A app.celery_app inspect ping` returns no responders.
- **P3-M — psql / pg_dump not on operator PATH** (NEW). Adjacent
  hygiene; surfaced repeatedly during diag work.
- **P3-P — dev-key storage hygiene** (NEW). `LUCIEL_PLATFORM_ADMIN_KEY`
  in operator Notepad rather than a credential manager.
- **P3-Q — `luciel-instance` admin DELETE returns 500 during teardown**
  (NEW). Anomaly observed in the 19/19 run; non-fatal (Pillar 10 still
  passed) but the 500 response is incorrect. Triage during Phase 3.

All five are logged in `docs/PHASE_3_COMPLIANCE_BACKLOG.md`.

---

## 7. What changed in the codebase, summarized

| File | Δ | Reason |
|---|---|---|
| `app/middleware/auth.py:124` | `user_id` → `actor_user_id` (1 line) | the actual fix |
| `app/middleware/auth.py:124..136` | 12-line forensic comment | future readers |
| `tests/middleware/test_actor_user_id_binding.py` | new (5 tests) | regression guard |
| `tests/middleware/__init__.py` | new (empty) | package marker |
| `app/middleware/auth.py:179..196` | removed (Commit D) | P13_DIAG no longer needed |
| `app/services/chat_service.py:440..514` | removed (Commit D) | P13_DIAG no longer needed |
| `diag_p13_repro.py` | deleted (Commit D) | reproduction served its purpose |
| `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` | new | evidence justifying instrumentation removal |
| `docs/verification-reports/README.md` | new | index + admissibility criteria |

Net code change: **+1 binding-fix line, +12 forensic-comment lines, +5 regression tests, −58 P13_DIAG lines, −269 diag script lines.**

---

## 8. Discipline reminders this session reinforced

- **One command per window per turn** (operator's standing rule).
  Honored throughout.
- **Three review gates before push** (build → user reviews diff →
  push). Honored for Commit A and Commit D.
- **No drive-by changes.** Trailing-newline issue in `auth.py` was
  pre-existing at HEAD; left untouched to keep the diff scoped.
- **Verification before claim.** 19/19 green run captured to JSON
  before instrumentation removal, not after.
- **Honest withdrawal.** Hypotheses 1, 2, 3 documented even though
  they did not contribute to the fix. The audit trail is the product.

---

**Status:** Commit A (`81b9e5a`) live on `step-28-hardening-impl`.
Commit D pending review of this recap + diff. After Commit D push:
- Drift register entry `D-pillar-13-a3-real-root-cause-2026-05-04`
  → marked RESOLVED.
- Phase 3 backlog gains P3-M, P3-N, P3-O, P3-P, P3-Q.
- Canonical recap bumps to v1.5.
- Phase 2 prod-touching work resumes: Commit 4 mint re-run via
  Option 3 ceremony, then Commits 5–7 (CloudWatch alarms, ECS
  auto-scaling, container healthchecks), then tag
  `step-28-phase-2-complete`.
