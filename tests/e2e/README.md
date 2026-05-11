# Per-step live end-to-end harnesses

This directory holds one harness per closed roadmap step. Each
harness exercises the **shipped production code paths** for that
step against the **literal success criteria** for that step's row
in `docs/CANONICAL_RECAP.md` §12.

Why this exists
---------------

The partner-level question at the boundary of every roadmap step is
"are we **actually** done?" — not "do the unit tests pass" but "if
we read the success-criteria column in the recap word-for-word, can
we demonstrate every clause against the running code?".

Unit tests answer "does this function behave as designed". They
mostly use inline fakes for speed and isolation. That is the right
discipline at the function level — but it leaves a gap: the success
criteria in `CANONICAL_RECAP.md` are written in product language
("a stray tool name is recorded as `unknown_tool`", "the gate
short-circuits APPROVAL_REQUIRED with a pending frame"), and a unit
test against an inline fake cannot directly assert "the shipped
broker, against the shipped registry, with the shipped tool
classes, satisfies that English claim".

The harnesses in this directory close that gap. Each one:

  * constructs the same objects the application constructs (real
    `ToolBroker`, real `ToolRegistry`, real shipped `LucielTool`
    subclasses — no inline fakes for the system under test),
  * runs a sequence of scenarios that each correspond to a sentence
    in the recap row,
  * prints a per-claim PASS/FAIL line and exits non-zero on any
    failure so a developer (or a future CI lane) can run it as a
    single command before tagging a step closed.

The first harness, `step_30c_live_e2e.py`, caught an audit-signal
collision (two operationally-different failure modes collapsing to
the same `tier_reason='unknown_tool'`) that all 28 unit tests
missed — because the unit tests were testing the function-level
behavior, and the bug was only visible when reading the metadata
trail through the recap's English-language audit claim.

How to run a harness
--------------------

Each harness is a standalone Python script:

    DATABASE_URL="sqlite:///:memory:" python tests/e2e/step_30c_live_e2e.py

Exit code 0 means every claim is satisfied. Non-zero means at least
one claim is violated — do NOT cut the step's closing tag until the
script is green.

How to write a new harness when closing a step
----------------------------------------------

1. Open `docs/CANONICAL_RECAP.md` §12 and find the row for the
   step you are closing. The cell labelled "How we'll know we're
   successful" is your assertion list.
2. Copy `step_30c_live_e2e.py` as a template (it documents the
   `ScenarioResult` / `record(...)` / `header(...)` pattern).
3. For every distinct claim in the success-criteria cell, add one
   numbered `CLAIM` section. Each section should:
     * construct the same production-shape objects the app
       constructs at boot (no inline fakes for the system under
       test — use the real `ToolBroker`, real config from
       `app.core.config.settings`, etc.),
     * execute the claim's behavior end-to-end,
     * record one or more boolean claims via `record(name, ok)`.
4. Run the harness. Iterate until all claims pass.
5. Commit the harness in the same branch as the step's closing
   commit.

When NOT to add a claim to a harness
------------------------------------

  * "It returns success=True for valid input" — that is a unit-test
    concern. Live-e2e is for **emergent properties** that only
    appear when the shipped pieces are wired together as the
    application wires them.
  * "It handles an unsupported character" — unit-test concern.
  * Anything that requires a live network, a live database with
    pre-seeded rows, or environment-specific secrets — that is what
    the widget-surface E2E CI lane is for
    (`.github/workflows/widget-e2e.yml`).

The per-step live-e2e harnesses are intentionally **backend-free**:
no Postgres, no Redis, no FastAPI TestClient, no live LLM. The
discipline is "what does this step claim, and can the harness
demonstrate it against the shipped Python without provisioning
infrastructure". A failure here is always a real production bug
or a real claim-vs-implementation drift, never an infra issue.

CI integration
--------------

Currently the harnesses are developer-runnable only — the AST +
unit-tests CI lane does NOT execute them. Wiring them into the lane
is a separate Pattern E decision that can be made when the
collection is large enough to justify the lane-time cost and when
the operational discipline (one harness per closed step) has held
across enough steps to prove the precedent. Until then, the contract
is: a step is not "closed" until its live-e2e harness in this
directory is green on the closing branch.

Closed steps with a live-e2e harness in this directory
------------------------------------------------------

| Step | Harness                       | Closing tag                                   |
|------|-------------------------------|-----------------------------------------------|
| 30c  | `step_30c_live_e2e.py`        | `step-30c-action-classification-complete`     |
