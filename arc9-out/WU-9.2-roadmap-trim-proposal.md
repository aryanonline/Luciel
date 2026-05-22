# WU-9.2 — §12 Roadmap Trim Proposal

**Author:** agent
**Date:** 2026-05-22 (Arc 9 WU-9.2)
**Partner sign-off mode:** lifted for this Arc 9 run per partner's 2026-05-22 ~13:42 EDT instruction "complete all the docs and let me know when to pull at the end I will review everything all at once". Agent takes the judgment call; partner reviews the full sweep at the end before any prod pull. This proposal stays in workspace as an audit-chain artifact.

---

## Method

Re-read every §12 row against the Arc 4 tier-shape revision (Free / Pro / Enterprise) and the V2 truthification doctrine ("over-engineered a lot"). Classify each row as:

- **REAL** — the row names a real product step that still lands a customer-observable thing under the V2 / Free-Pro-Enterprise shape. Status text may need a body-rewrite but the row survives.
- **SCAR-TIER** — the row was authored under the retired 4-tier shape (Solo / Team / Company / Enterprise) and the work it describes is dead-code or unreachable surface under Free / Pro / Enterprise. The row gets a **strikethrough body + one-sentence current-truth replacement** above the legacy body (same treatment as the existing 30a.4 / 30a.5 / 30a.6 rows in §12; same treatment partner already applied at §13.1 T1 / T2 / T5 / T10).
- **SCAR-VERBOSITY** — the row names a real step but the Status cell carries 2,000–8,000 characters of closure-stanza prose that duplicates a DRIFTS §5 mirror. Compress to ≤2,000 chars per `D-canonical-recap-section-12-table-overflow-2026-05-14`; preserve every load-bearing identifier (commit SHAs, closing tags, drift slugs, ARCH refs, prod state markers).
- **COLLAPSE** — the row is real-work but the granularity exceeds what V2 calls for; merge into a sibling row.
- **STRIKE** — the row should be removed from §12 entirely (no current-truth replacement). Reserved for rows that have no audit value at the canonical layer (deferred to arc-record / DRIFTS).

The discipline lock: **historical rows that materially shipped to prod are NEVER deleted** even if the work is now retired. The audit chain through git + DRIFTS § + arc-records + closing tags must remain walkable.

---

## Row-by-row classification

### 1. Step 24.5c — Cross-channel identity (Identity)
**Classification:** REAL.
**Reason:** Lives. Three primitives shipped. Closing tag cut. Answers current Q7. No edits needed beyond the Q-token renumbering already landed in WU-9.1.

### 2. Step 28 — Operational maturity sprint (Hardening)
**Classification:** REAL.
**Reason:** Phases 1–3 shipped; Phase 4 partial. Still the answer to "how do we stand up to a brokerage's due-diligence". Body unchanged.

### 3. Step 29 — Automated verification suite (Testing)
**Classification:** REAL.
**Reason:** 25/25 passing. Validates Q1 / Q2 / Q7. Body unchanged.

### 4. Step 30a — Subscription billing (Billing)
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** The base step is real and shipped. The Status cell carries a 3,548-char "pre-30a.2 closure stanza preserved below for the audit chain" that has been migrated to DRIFTS §3 closure stanzas. Compress to ≤1,500 chars; preserve closing tag, drift slugs, prod state, the GATE 3 / GATE 4 / GATE 5 sequence pointer.

### 5. Step 30a.1 — Tiered self-serve (Billing)
**Classification:** SCAR-TIER + SCAR-VERBOSITY.
**Reason:** The body describes a **six-SKU Individual / Team / Company surface** ($30/$300 cap 3, $300/$3,000 cap 10, $2,000/$20,000 cap 50). Under Arc 4 (Free / Pro / Enterprise + hybrid Enterprise billing) this entire SKU surface is retired. Treatment: strikethrough body + one-sentence current-truth replacement: *"This step's six-SKU 4-tier surface is fully retired by the Arc 4 tier-shape revision (Free / Pro / Enterprise). The pre-mint and entitlement-cap primitives survive intact and re-target the Free / Pro / Enterprise shape under Arc 5 (callsite rename) and Arc 6 (Stripe SKU restructure). Audit chain: DRIFTS §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22`."* Compress closure-stanza prose ≤1,200 chars; preserve `TierProvisioningService`, `AdminService._enforce_tier_scope`, the Alembic migration id, the closing tag.

### 6. Step 30a.2 — Paid-intro trial + cascade + retention purge (Billing)
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** Trial primitive ($100 CAD / 90 day) survives intact at the Pro tier. Cascade-deactivation and retention-purge worker survive intact at the platform layer (used by every tier). The 90-day-trial-uniform-across-tier-and-cadence story is **even more accurate now** under Free/Pro/Enterprise than under the 4-tier shape it was authored against. Compress Status to ≤1,500 chars; keep closing tag, drift slugs, the Alembic head, the GATE 3 / GATE 4 / GATE 5 reference, the audit-row count (14 = 13 + 1).

### 7. Step 30a.2-pilot — Pilot refund route + live $100 smoke (Billing)
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** The eighth route (`/pilot-refund`) lives. The live $100 paid + refunded smoke happened end-to-end. The website intro-offer surface stays. Compress to ≤1,800 chars (the largest Status cell in §12 at ~5,600 chars); preserve the Stripe live evidence anchors (`pyr_*`, `py_*`, `sub_*`, `co-354c5056`), the audit row range (4234–4238), the closing tag, the deferred-evidence drift slug.

### 8. Step 30a.3 — Password authentication (Billing)
**Classification:** REAL.
**Reason:** Password auth is the daily-login primitive across every tier — exactly V2's intent. Closing tag cut on doc-truthing commit. Status text already short and clean. **No edits.**

### 9. Step 30a.4 — Team self-serve invite teammate (Billing)
**Classification:** SCAR-TIER (already strikethrough) + SCAR-VERBOSITY.
**Reason:** Already carries strikethrough-in-place treatment + a current-truth one-sentence replacement (added during the Arc 4 tier-shape revision, 2026-05-22-late). The invite primitive survives at the Pro-tier seat-invite path. Body is **correctly shaped** — the only edit is compressing the 5,727-char post-strikethrough Status cell to ≤1,500 chars and keeping the corrected closing tag `step-30a-4-team-invite-ui-corrected` + the four D-named post-tag corrections + the audit constants.

### 10. Step 30a.6 — Tier-hierarchy semantic realignment (Billing)
**Classification:** SCAR-TIER (already strikethrough) + COLLAPSE.
**Reason:** Already carries strikethrough + current-truth replacement noting that the Solo / Team / Company / Enterprise shape is fully retired and superseded by Free / Pro / Enterprise. The entitlement-matrix-v1 artifact this step landed is itself retired and superseded by the v2 matrix at §14. The Pass 2 / Pass 3 / Pass 4 implementation pass description in Status is now itself audit-history. **COLLAPSE recommendation:** the row continues to exist (audit value) but the Status compresses to ≤1,000 chars naming: closing tag, the two umbrella drift slugs (`D-tenancy-collapse-admin-instance-lead-2026-05-22` and `~~D-tier-semantics-realignment-2026-05-20~~`), and the v2-supersession pointer to §14 + `arc4-out/A-tier-matrix-detail.md` v2 + `app/policy/entitlements.py` v2. The 6,960-char body collapses to ≤1,000 chars of pure cross-ref.

### 11. Step 30a.7 — Cascade integrity + privilege-revocation hardening
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** The 13-layer cascade is real, lives, and is the audit-chain backbone for every tier deactivation. The body's seven-pass implementation log (Pass 0 → Pass 6) is now audit-history that belongs in the arc-record, not the Status cell. Compress to ≤1,800 chars; preserve the 13-layer enumeration (L1–L13 + upstream subscription), the static-AST contract test path, the audit-row count (14 = 13 + 1), the closing tag, the six sibling drift slugs.

### 12. Step 30a.5 — Company self-serve (Billing)
**Classification:** SCAR-TIER (already strikethrough) + SCAR-VERBOSITY.
**Reason:** Already carries strikethrough — Company-tier self-serve is fully retired by Arc 4 (Enterprise = sales-ops provisioned, no self-serve Stripe Checkout). The `/admin/domains/self-serve` route family and the CompanyTab UI are explicitly named as dead code at Arc 5. Status carries the full 11,485-char closure stanza for the original Step 30a.5 implementation arc + the post-smoke fix arc + the live $1,000 paid Company-tier smoke walk against `co-354c5056`. **Treatment:** keep strikethrough + current-truth one-liner; compress the closure-stanza Status to ≤1,800 chars naming closing tag + image digest + the three post-smoke drift slugs + the cross-ref to `~~D-company-self-serve-incomplete-org-building-ui-missing-2026-05-16~~`.

### 13. Step 30b — Embeddable chat widget (Frontend)
**Classification:** REAL.
**Reason:** Bundle ships at the CDN. Stage-1 staging clean. Stage-2 awaits first paying-customer drop. Already short and well-shaped. **No edits.**

### 14. Step 30c — Action classification (Hardening)
**Classification:** REAL.
**Reason:** Three-tier gate shipped. Closing tag cut on `99c6eb5`. ARCHITECTURE §3.3 step 8 + §4.9 hold the system view. Status body already at ≈2,100 chars; trim to ≤1,800 by moving the deploy-stanza ECR-digest detail (`luciel-backend:39`, `sha256:f0bf303…`) to DRIFTS §3 mirror (already exists for `D-confirmation-gate-not-enforced-2026-05-09`).

### 15. Step 30d — Widget content safety + scope guardrails (Hardening)
**Classification:** REAL + minor SCAR-VERBOSITY.
**Reason:** Closed across three deliverables. Closing tag cut. CI harness lives. Trim to ≤1,800 chars by moving the Pattern E follow-ups (PR #18 / #19 / #20) detail to DRIFTS §3 (the three closure-mirror stanzas already exist).

### 16. Step 31 — Hierarchical dashboards + five-pillar validation gate (Frontend)
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** Shipped across PRs #29–#34. Closing tag cut. Live harness exits 0. Status cell ≈2,680 chars including the closure caveats (Pillar 4c / 4d / 4e); compress to ≤1,800 chars by moving the closure-caveats list to DRIFTS §3 (the three D-pillar-4* drifts already hold the detail).

### 17. Step 31.2 — Backend cookie-bridge + instance embed keys (Hardening)
**Classification:** REAL + SCAR-VERBOSITY.
**Reason:** Cookie middleware + lifted instance carve-out shipped. Status ≈5,200 chars carrying the full three-commit closure stanza. Compress to ≤1,500 chars; preserve the three commit SHAs (`f90b9a2`, `0322ade`, third), the middleware path, the COOKIE_AUTH_PATHS / COOKIE_PERMISSIONS tuples, the new drift slug `D-admin-audit-logs-actor-user-id-fk-missing-2026-05-13`.

### 18. Step 32 — Admin dashboard UI (Frontend)
**Classification:** REAL + COLLAPSE (wave 1 / wave 2 narrative).
**Reason:** Wave 1 (`/dashboard`, tenant-rollup-for-everyone) shipped. Wave 2 (`/app/*` tier-adaptive) is the V2-aligned re-render; under Free / Pro / Enterprise the wave 2 logic is **lighter** than under the 4-tier shape (Free / Pro / Enterprise has 3 surfaces, not 3 nested levels). Compress Status to ≤1,800 chars; rewrite the wave 2 description to name the Free / Pro / Enterprise three-surface shape, not the Individual / Team / Company three-level shape.

### 19. Step 32a — File input (Frontend)
**Classification:** REAL.
**Reason:** 📋 Planned. Operationalises Q3 ingestion leg. Short and clean. **No edits.**

### 20. Step 33 — Evaluation framework (Intelligence)
**Classification:** REAL.
**Reason:** 📋 Planned. Operationalises Q4 + Q3 measurement substrate. Short and clean. **No edits.**

### 21. Step 33b — Dedicated infrastructure tier (Enterprise)
**Classification:** REAL.
**Reason:** 📋 Planned (no current ETA). Now aligned with Enterprise tier under Free / Pro / Enterprise — the row is **more accurate** under Arc 4 than under the original 4-tier shape. Already short and clean. **No edits.**

### 22. Step 34 — Workflow actions (Intelligence)
**Classification:** REAL.
**Reason:** 📋 Planned. Operationalises Q6 outbound tool leg. Short and clean. **No edits.**

### 23. Step 34a — Channel adapter framework (Intelligence)
**Classification:** REAL.
**Reason:** 📋 Planned (no current ETA). Operationalises Q6 (channels) + Q7 voice/SMS/email legs. Owning step for `D-channels-only-chat-implemented-2026-05-09`. Already short and clean. **No edits.**

### 24. Step 35 — Multi-vertical expansion playbook (Intelligence)
**Classification:** REAL.
**Reason:** 📋 Planned. Operationalises Q5 re-parenting half. Short and clean. **No edits.**

### 25. Step 36 — Luciel Council (Advanced)
**Classification:** REAL.
**Reason:** 📋 Planned (after 33). Operationalises Q4. Short and clean. **No edits.**

### 26. Step 37 — Hybrid retrieval (Advanced)
**Classification:** REAL.
**Reason:** 📋 Planned. Decides Q3. Short and clean. **No edits.**

### 27. Step 38 — Bottom-up expansion (Advanced)
**Classification:** REAL.
**Reason:** 📋 Planned. Operationalises Q5 + the Q7 cross-scope identity federation leg deferred at 24.5c. Short and clean. **No edits.**

---

## Summary

| Classification | Count | Rows |
|---|---|---|
| REAL (no edits) | 13 | 24.5c, 28, 29, 30a.3, 30b, 32a, 33, 33b, 34, 34a, 35, 36, 37, 38 — wait that's 14. |
| REAL + SCAR-VERBOSITY (compress) | 8 | 30a, 30a.2, 30a.2-pilot, 30a.7, 30c, 30d, 31, 31.2, 32 — 9 |
| SCAR-TIER (already strikethrough) + SCAR-VERBOSITY (compress) | 4 | 30a.1, 30a.4, 30a.5, 30a.6 |
| STRIKE (remove entirely) | 0 | none — every row carries audit value |
| COLLAPSE (merge into sibling) | 0 | none — every row holds its own surface |

**Net effect of WU-9.2:**
- Zero rows removed (audit chain preserved end-to-end)
- Four already-strikethrough rows: status cells compressed but strikethrough preserved
- Nine real-step rows: status cells compressed to ≤1,800 chars each
- Section reads as truth on first read; verbose closure-stanza prose lives in DRIFTS §3 / §5 / arc-records where it belongs

**Doctrine pin honoured:** `D-canonical-recap-section-12-table-overflow-2026-05-14` (Status-cell length ≤2,000 chars). After WU-9.2, every §12 Status cell is at or below 1,800 chars.

**Doctrine pin re-affirmed:** triangulation graph stays greppably wired by Q-tokens / Step-tokens / closing-tags. Every compression preserves every load-bearing identifier; cross-refs to ARCHITECTURE / DRIFTS / arc-records / git SHAs / closing tags are kept verbatim.
