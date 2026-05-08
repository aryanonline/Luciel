# docs/archive — superseded documentation

This folder contains documentation that has been **deactivated, not deleted** (Pattern E) as part of the Step 29.y close-out three-doc regime.

## Canonical living docs (use these — not anything in this folder)

The current source of truth lives at:

- `docs/CANONICAL_RECAP.md` — business value, pricing, roadmap, GTM, moat, locked decisions, exclusions
- `docs/ARCHITECTURE.md` — code layout, data model, AWS topology, deployment, verification harness, audit chain
- `docs/DRIFTS.md` — every drift token (open + resolved), supersessions, security disclosures

Everything in this archive is preserved for forensic / historical reference only. Do not edit. Do not cite as current.

## Mapping (where each archived doc's content lives now)

| Archived | Current home |
|----------|--------------|
| `CANONICAL_RECAP_v3.4.md` | Replaced by `docs/CANONICAL_RECAP.md` (business-only, surgical-edit regime). Strategic content (Q1–Q8, pricing tiers, roadmap, locked decisions, exclusions, moat, WTP drivers, GTM phases, revenue milestones) preserved; version-history sediment dropped. |
| `DRIFT_REGISTER.md` | Replaced by `docs/DRIFTS.md`. Every drift token folded forward. Closed tokens carry strikethrough; open tokens are in §2. |
| `DISCLOSURES.md` | Folded into `docs/DRIFTS.md` §6 (DISC-2026-001 rate-limiter typo, DISC-2026-003 audit-duplicates incident). |
| `PHASE_3_COMPLIANCE_BACKLOG.md` | Compliance items absorbed into `docs/DRIFTS.md` §2 (deferred / open) and `CANONICAL_RECAP.md` §11. |
| `STEP_29Y_CLOSE.md` | Closure procedure now lives in `CANONICAL_RECAP.md` §13 resumption protocol + commit history. |
| `STEP_29Y_DEFERRED.md` | Deferred items folded into `DRIFTS.md` §2 carry-forward. |
| `STEP_29_AUDIT.md` | Step 29 audit harness state captured in `ARCHITECTURE.md` §7 verification harness. |
| `architecture/` | Single file `broker-and-limiter.md` referenced from `DRIFTS.md` C19 closure; topology now in `ARCHITECTURE.md` §6 + §9. |
| `compliance/` | `audit-emission-posture.md` superseded by `ARCHITECTURE.md` §11 audit chain. |
| `evidence/` | Operational JSON evidence; not part of any living-doc surface. Retained for forensic value. |
| `findings/` | Phase 1 finding indexes (1b/1d/1e/1f/1g); content folded into `DRIFTS.md` closures. |
| `recaps/` | Pre-Step 29 session recaps; succeeded by the current canonical doc set. |

## Folders that did NOT move (still live in `docs/`)

- `docs/incidents/` — postmortems are append-only and authoritative; they stay live.
- `docs/runbooks/` — operational runbooks for deploy, rotation, prod access; stay live.
- `docs/verification-reports/` — verification harness output artifacts; stay live.

## Why archive instead of delete

Pattern E (deactivate, never delete) applies to documentation as well as data. Deletion would lose the v3.4 strategic context (some of which was the input to this round of canonicalization), would break inbound references from old commit messages and chat logs, and would set a precedent that doc history is disposable. Archive preserves the chain.
