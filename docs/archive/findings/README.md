# Luciel Phase-1 Findings — Reconstruction Index

**Drift token:** `D-findings-docs-not-in-repo-2026-05-07`
**Established by:** Step 29.y gap-fix Commit 5
**Branch of origin:** `step-29y-gapfix` off `step-29y-impl` HEAD `a98525a`

## Why this directory exists

The Luciel codebase has 17 in-source references (across 11 files) to `findings_phase1b.md`, `findings_phase1d.md`, `findings_phase1e.md`, `findings_phase1f.md`, and `findings_phase1g.md`. Those documents describe security and integrity audit findings (e.g. `B-1`, `D-8`, `D-9`, `E-2`, `E-3`, `E-5`, `F-7`, `G-1` … `G-7`) and motivate large parts of the Step 29.y work.

**The original `findings_phase1*.md` documents are not in the repository.** They predate the current branch HEAD, and at the time of writing they cannot be located in any branch, tag, or commit history of the Luciel project. The user explicitly confirmed (re Cluster 7) that the source documents are no longer available.

Per the Luciel working doctrine, source-of-truth precedence is:

> code > commit > canonical recap > prior recaps > chat

This directory respects that precedence. We do **not** reconstruct the missing documents from chat or memory. Instead, we build a navigable index from the highest-precedence sources we still have: **the code that cites the findings, and the commits that resolve them.**

## How to use this index

When you encounter a citation in the code such as `# See findings_phase1g.md G-3`, do the following:

1. Open the per-phase index file (e.g. [`phase1g.md`](./phase1g.md)).
2. Find the finding token (e.g. `G-3`).
3. Read:
   - the **code citations** (every file:line that references it)
   - the **resolution commits** (every commit on `step-29y-impl` that addresses it)
   - the **summary** reconstructed from those two sources

This is the canonical answer to "what was finding X about, and how did we resolve it." It is more reliable than chat-history reconstruction because it is anchored in code and git history.

## Per-phase indexes

- [phase1b.md](./phase1b.md) — Rate-limit fail-mode (`B-1`)
- [phase1d.md](./phase1d.md) — Audit-chain integrity (`D-8`, `D-9`)
- [phase1e.md](./phase1e.md) — Worker hardening (`E-2`, `E-3`, `E-5`; `E-6`, `E-12`, `E-13` deferred to Step 30c)
- [phase1f.md](./phase1f.md) — Sessions / FK integrity (`F-7`)
- [phase1g.md](./phase1g.md) — PIPEDA scope hardening (`G-1`, `G-2`, `G-3`, `G-4`, `G-5`, `G-6`, `G-7`)

## What about Cluster 7?

Cluster 7 has no commits, no tests, and no code citations. The commit log on `step-29y-impl` jumps directly from Cluster 6 to Cluster 8. Per gap-fix Commit 7 (`D-cluster-7-unaccounted-2026-05-07`), this is logged in the drift register as "investigated, no evidence on branch." See [`docs/STEP_29Y_DEFERRED.md`](../STEP_29Y_DEFERRED.md) for the full disposition.

## Maintenance contract

When a future commit adds a `findings_phase1X.md TOKEN` reference to code, the author MUST add a corresponding entry to the matching per-phase index here in the same commit. Pillar 23 cannot enforce this — it is a doctrine commitment.

When the original `findings_phase1*.md` documents are recovered (if ever), they should be checked in alongside this index, not in place of it. The reconstruction view is independently useful: it traces the lifecycle from finding to resolution to verification, which the original documents may not.
