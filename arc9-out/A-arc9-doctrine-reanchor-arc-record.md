# Arc 9 — Doctrine Re-Anchor execution arc record

**Status:** OPEN — doctrine-only arc. Zero code commits until Arc 9 closes. Symmetry across the canonical trio (CANONICAL_RECAP ↔ ARCHITECTURE ↔ DRIFTS) is the load-bearing rule for every work-unit in this arc.

**Authored:** 2026-05-22 at the close of the Arc 8 WU-2/WU-3 ceremony, immediately after the partner's V2 reconstruction (`docs/in-flight/Luciel-V2-canonical-input-2026-05-22.md`) surfaced over-engineering across the canonical trio.

**Audience:** Partner-facing (this is a doctrine arc, not a code arc). Every work-unit lands as a commit-shaped doc edit reviewed by the partner before push.

**Triggering statement (partner, 2026-05-22 ~13:17 EDT):** *"I hope we update and properly configure our docs to support our vision so we can continue with our roadmap. We might need to modify the roadmap a little to support our vision. We have over engineered a lot."* And immediately after (~13:20 EDT): *"your framing sounds correct. I hope we still maintain the discipline and symmetry between our docs properly. I would let us truthify the docs and then we can get to shipping the right things again."*

**Cross-refs:**
- `docs/in-flight/Luciel-V2-canonical-input-2026-05-22.md` — partner's V2 reconstruction (doctrine source-of-record for this arc)
- `docs/CANONICAL_RECAP.md` — primary target of WU-9.1 / 9.2 / 9.3 / 9.4
- `docs/ARCHITECTURE.md` — primary target of WU-9.5
- `docs/DRIFTS.md` — primary target of WU-9.6 + this arc's umbrella drift `D-arc9-doctrine-reanchor-umbrella-2026-05-22`
- `arc4-out/A-tenancy-collapse-arc-record.md` — Arc 4 record this arc re-scopes (WU-9.7 trims it against V2)
- `arc4-out/A-tier-matrix-detail.md` — Arc 4 Deliverable #2 this arc re-scopes (WU-9.3 simplifies 18-dim matrix)

---

## §1 — Scope and non-scope

### §1.1 — In scope (this arc's commits land these)

1. **WU-9.1 — V2 absorb into CANONICAL_RECAP §1–§11.** Mechanical pass since V2 is the partner's own writing. Sections that V2 sharpens get rewritten verbatim; sections V2 leaves alone stay.
2. **WU-9.2 — Roadmap trim of §12.** Identify which Step 24.5c → 38 rows are real product decisions, which are scar tissue (corrections to corrections), and which collapse into one another under V2's flat Admin→Instance→Lead model. Partner sign-off required before landing.
3. **WU-9.3 — §14 simplification.** Strip the 18-dimension × 3-tier entitlement matrix to the abstract domain-agnostic shape V2 explicitly calls for (V2 §14 ¶1: *"We need to make this a bit abstract if we are considering domain agnostic and model agnostic"*). Per-dimension implementation detail moves to ARCHITECTURE where it belongs.
4. **WU-9.4 — §13 T-scenarios rewrite.** Address V2 §13 ¶1037 FYI: *"We need to rewrite and structure the entire section 13 based on our new vision."*
5. **WU-9.5 — ARCHITECTURE re-anchor.** Strip legacy four-tier scaffolding (Solo/Team/Company/Enterprise residuals, Tenant→Domain→Agent→LucielInstance hierarchy references in design prose). Mark every "Live today" vs "Designed but not built" boundary honestly. The §3.2.x and §4.x section tree is preserved; the prose inside each section gets truth-tested.
6. **WU-9.6 — DRIFTS reconciliation.** Re-read every OPEN drift in §3. Three outcomes per drift: (a) **COLLAPSED** — the shape the drift defended no longer exists under V2, drift closes with "collapsed-by-Arc-9" note; (b) **SHARPENED** — V2 makes the drift more concrete or higher-priority, drift entry rewritten; (c) **UNCHANGED** — drift is orthogonal to V2 reshaping, no edit. NEW drifts opened where V2 surfaces previously-unnamed gaps.
7. **WU-9.7 — Arc 5 / Arc 6 scope revision.** Rewrite `arc4-out/A-tenancy-collapse-arc-record.md` and `arc4-out/A-tier-matrix-detail.md` against V2. Expected outcome: substantial shrinkage. Many of the corrections-to-corrections accumulated in Steps 30a.1 → 30a.7 disappear under V2's collapsed model.
8. **WU-9.8 — Arc 9 close + symmetry verification.** Triangulation check: every fact in §11/§12/§14 of CANONICAL_RECAP has a matching shape in ARCHITECTURE §3/§4 and a matching integrity row in DRIFTS §3. Doctrine reads as truth on first read end-to-end. Closing tag: `arc-9-doctrine-reanchor-complete`.

### §1.2 — Explicitly out of scope (deferred until Arc 9 closes)

1. **All code commits.** Zero `app/`, `tests/`, `alembic/`, `scripts/`, `Dockerfile`, `deploy_*.ps1` edits. Doctrine first; code follows once the doctrine is truth.
2. **Arc 8 WU-6 (SES feedback / suppression).** The two SES drifts (`D-ses-feedback-loop-not-wired-2026-05-22`, `D-ses-suppression-app-layer-not-implemented-2026-05-22`) survive Arc 9 because the SES surface is orthogonal to tier-shape doctrine. WU-6 resumes immediately after Arc 9 closes.
3. **Arc 5 schema migration.** Deferred until the Arc 9 doctrine rewrite produces the final V2-anchored Alembic plan. The current Arc 4 Deliverable #3 sequence (Revisions A/B/C) will be re-scoped at WU-9.7 against V2; the revised plan is what Arc 5 executes.
4. **Arc 6 Stripe SKU restructure.** Deferred for the same reason — V2's three-tier shape (Free $0 / Pro $30 / Enterprise hybrid) is preserved from the live canonical, but the entitlement-matrix-driven Price set may shrink substantially under WU-9.3 simplification.
5. **Phase 6 Pass 0 E2E replay.** Deferred until Arc 5 + Arc 6 land against the V2 doctrine.

---

## §2 — Why this arc exists (the over-engineering audit)

The partner's V2 reconstruction surfaced five over-engineering signals across the canonical trio. Each signal is named here as a discrete defect class so WU-9.6 (DRIFTS reconciliation) and WU-9.7 (Arc 5/6 scope revision) have concrete targets to act on.

### §2.1 — Defect class A: Four-tier scar tissue across the code despite the 2026-05-22 doctrine collapse

The Admin→Instance→Lead tenancy collapse landed in doctrine on 2026-05-22 (umbrella drift `D-tenancy-collapse-admin-instance-lead-2026-05-22`), but the code still carries the legacy four-level hierarchy: `tenants` / `domains` / `agents` / `luciel_instances` tables, ~4,025 callsites pending rename, cascade chain at 13 layers (Step 30a.7) defending teardown of a hierarchy V2 collapses. Code complexity defending a shape doctrine has retired.

### §2.2 — Defect class B: Seven sub-steps layered onto what V2 treats as one product shape

Steps 30a.1 / 30a.4 / 30a.5 / 30a.6 / 30a.7 are corrections-to-corrections-to-corrections on a four-tier shape V2 collapses to one shape. Many of these steps shouldn't exist as named entries in canonical §12 — they're scar tissue from building the wrong shape and re-shaping it. V2 §12 doesn't carry them; live canonical §12 carries seven sub-steps under Step 30a.

### §2.3 — Defect class C: §14 entitlement matrix expanded to 18 dimensions × 3 tiers

The live §14 entitlement matrix carries 18 dimensions across 3 tiers (instance caps, leads caps, conversation caps, rate limits, channel adapters, branding, SSO, SLA, success manager, retention class, composition depth, etc.). V2 §14 has 4 columns (Tier / Monthly / Annual / Instance cap / Features) and an explicit partner annotation: *"We need to make this a bit abstract if we are considering domain agnostic and model agnostic."* Two orders of magnitude too much detail in the buyer-facing canonical doc; per-dimension detail belongs in ARCHITECTURE where it's an implementation concern, not in §14 where it's a buyer-trust concern.

### §2.4 — Defect class D: Three-pass cascade-layer correction defending a teardown shape V2 doesn't need

Step 30a.2 landed a 9-layer cascade. Step 30a.7 corrected it to 13 layers. Under V2's Admin→Instance→Lead collapse and zero in-flight customers, that defense-in-depth complexity is engineering against a hierarchy that's being removed. The cascade layers themselves are not wrong — privilege-bearing rows do need teardown — but defending the *count* at 13 is signal that the model itself is over-fitted.

### §2.5 — Defect class E: Same-Admin tier transition doctrine hole (partner-surfaced 2026-05-22-late)

V2 Q1 makes explicit what live canonical Q1 leaves implicit: *"The Admin should be able to upgrade or downgrade between tiers without having to restart or lose upon their work."* This is **bidirectional** tier transition (upgrade AND downgrade), same-Admin, on the same account row. Live canonical Q5 only covers cross-Admin re-parenting upward. The drift `D-same-admin-tier-transition-doctrine-hole-2026-05-22` filed at commit `c90b9f2` opened this gap but V2 makes it canonical doctrine.

---

## §3 — Work-unit plan

Each work-unit lands as one or two commits, doc-only. Partner sign-off precedes every push except WU-9.1 (mechanical V2 absorb is partner's own writing).

### §3.1 — WU-9.1 — V2 absorb (mechanical)

**Input:** `docs/in-flight/Luciel-V2-canonical-input-2026-05-22.md` (preserved at Arc 9 open).
**Output:** `docs/CANONICAL_RECAP.md` §1–§11 rewritten to absorb V2 verbatim where V2 sharpens; live canonical sections V2 leaves alone are preserved.
**Method:** Section-by-section diff between live canonical and V2. V2 wins on every conflict (partner authored V2 as truth). Status-marker semantics from live canonical preserved (✅ / 🔧 / 📋 / 🔬). Drift cross-refs from live canonical preserved.
**Partner involvement:** No sign-off required for the mechanical absorb. Sign-off lands on the commit-message review before push.
**Estimated diff size:** ±300 lines across §1–§11.

### §3.2 — WU-9.2 — Roadmap trim of §12

**Input:** Live `docs/CANONICAL_RECAP.md` §12 (20+ step rows) + V2 §12 (which carries fewer rows by intent — the V2 §12 is mostly preserved from live but the FYI markers signal partner intent to simplify).
**Output:** §12 with per-row classification: REAL / SCAR / COLLAPSE-INTO-PARENT. Scar rows get archived to a new appendix §12.Z "Retired roadmap rows" with one-line provenance pointers to git. Collapsed rows fold into their parent step.
**Partner involvement:** **Required sign-off before push.** The proposal commit lands the classified §12 as a draft block; partner reviews, signs off, then the cleanup commit removes the scar rows and lands the simplified §12.
**Estimated outcome:** §12 shrinks from ~30 visible rows to ~12 visible rows + appendix.

### §3.3 — WU-9.3 — §14 simplification

**Input:** Live §14 (18-dim × 3-tier entitlement matrix) + V2 §14 (4-column abstract shape) + partner annotation *"to fill properly"* + *"abstract if domain agnostic and model agnostic"*.
**Output:** §14 rewritten to V2's 4-column shape with abstract domain-agnostic feature language. The per-dimension entitlement matrix moves to ARCHITECTURE §3.2.x (implementation concern). The §14 buyer-facing copy speaks in product values, not enforcement axes.
**Partner involvement:** **Required sign-off before push.** I'll propose the abstract feature copy per tier (Free / Pro / Enterprise) and partner approves wording.

### §3.4 — WU-9.4 — §13 T-scenarios rewrite

**Input:** Live §13 (T1–T19 with 2026-05-22-late annotation patches layered onto legacy T1/T2/T5/T10 framing) + V2 §13 FYI (*"we need to rewrite and structure the entire section 13 based on our new vision"*).
**Output:** §13 rewritten end-to-end against V2's Admin→Instance→Lead + Free/Pro/Enterprise model. T1–T8 still map to Q1–Q8; T9–T19 still cover cross-cutting customer journey + behavior contracts. Story prose freshly written, not annotation-patched.
**Partner involvement:** **Required sign-off before push.** Story-language calls partner judgment.

### §3.5 — WU-9.5 — ARCHITECTURE re-anchor

**Input:** Live `docs/ARCHITECTURE.md` + V2-anchored §1–§14 of CANONICAL_RECAP from WU-9.1–9.4.
**Output:** ARCHITECTURE re-anchored so every §3.2.x and §4.x section reads as truth against the V2 model. Legacy four-tier prose stripped. "Live today" vs "Designed but not built" boundary marked honestly per section. The 18-dim per-tier matrix from WU-9.3 lands here at §3.2.14 (or new §3.2.15).
**Partner involvement:** Sign-off per section, not per commit. ARCHITECTURE is long enough that partner reviews in chunks.

### §3.6 — WU-9.6 — DRIFTS reconciliation

**Input:** Live `docs/DRIFTS.md` §3 (all OPEN drifts).
**Output:** Every OPEN drift classified COLLAPSED / SHARPENED / UNCHANGED. COLLAPSED drifts close with "collapsed-by-Arc-9" note + closing-tag-equivalent. SHARPENED drifts rewritten. NEW drifts opened where V2 surfaces gaps. Drift count expected to **decrease** substantially (a sign Arc 9 worked).
**Partner involvement:** Sign-off on the COLLAPSED list before closures land (partner gets to veto any drift I propose to close).

### §3.7 — WU-9.7 — Arc 5 / Arc 6 scope revision

**Input:** Live `arc4-out/A-tenancy-collapse-arc-record.md` (Arc 5 + Arc 6 execution plan) + `arc4-out/A-tier-matrix-detail.md` (entitlement matrix v2) + Arc 9 doctrine output from WU-9.1–9.6.
**Output:** Both Arc 4 artifacts rewritten against V2. Expected: Arc 5 schema migration shrinks (fewer cascade layers, fewer entitlement dimensions, no `domains` table renames because the table is being dropped); Arc 6 Stripe SKU restructure stays roughly the same shape (Free + Pro + Enterprise hybrid) but with fewer per-tier dimensions to wire.
**Partner involvement:** Sign-off on the revised Arc 5 + Arc 6 plans before they land.

### §3.8 — WU-9.8 — Arc 9 close + symmetry verification

**Input:** All doc state at end of WU-9.7.
**Output:** Triangulation pass — every fact in CANONICAL §11/§12/§14 cross-referenced against ARCHITECTURE §3.2.x/§4.x and DRIFTS §3 entries. Symmetry confirmed. Closing tag `arc-9-doctrine-reanchor-complete` stamps on the final WU-9.8 commit.
**Partner involvement:** Final sign-off lands the closing tag.

---

## §4 — Discipline and symmetry locks (carry-forward for every WU)

These rules govern every doc edit in Arc 9. Violating any of them re-opens the work-unit.

1. **Three-doc triangulation.** Every fact must appear in at least two of {CANONICAL_RECAP, ARCHITECTURE, DRIFTS}, from each doc's own angle. CANONICAL is the buyer-facing source; ARCHITECTURE is the engineer-facing source; DRIFTS is the integrity-and-debt source.
2. **Truth on first read.** Doctrine docs read as current truth at every line. Annotations belong in DRIFTS and arc-records, not in CANONICAL or ARCHITECTURE.
3. **No version-history sediment.** Past doctrine state lives in git and in DRIFTS audit chain. Current state lives in CANONICAL / ARCHITECTURE.
4. **Surgical edits only.** No mass rewrites that obscure provenance. Every WU's commit message names exactly what changed and why.
5. **Partner sign-off where it matters.** Mechanical absorbs (WU-9.1) ship without per-section sign-off. Judgment calls (WU-9.2 / 9.3 / 9.4 / 9.7) require sign-off before push.
6. **Doc-only.** Zero code commits in Arc 9. The first code commit lands post-Arc-9 against the V2-anchored doctrine.

---

## §5 — Audit chain entry points

This arc opens at commit (TBD on Arc 9 open commit). Closes at the WU-9.8 commit stamping `arc-9-doctrine-reanchor-complete`. Every WU commit references this arc-record by path. The umbrella drift `D-arc9-doctrine-reanchor-umbrella-2026-05-22` (filed at Arc 9 open) carries the running status table updated per WU close.
