"""Step 26 — Child Luciel Assembly Verification.

End-to-end proof that Steps 21 through 25b work together as one cohesive
whole. Go/no-go gate for Step 26b production redeploy and GTA outreach.

This is the redo of commit 85a29f3 (`scripts/step26_verify.py`), migrated
to a proper subpackage so the runner, fixtures, and pillars are each
replaceable units. See `__main__.py` for the entry point and `runner.py`
for the orchestrator contract.

Invariants preserved from the landed suite:
  - Fresh throwaway tenant per run (`step26-verify-<uuid8>`)
  - Run-all-then-report (one failure does not stop the matrix)
  - Teardown always fires unless --keep
  - Migration integrity runs post-teardown (read-only)
  - Exit 0 iff every pillar passed

Gaps closed in the redo (see docstrings on individual pillars):
  1. Phantom MAGIC_TOKEN -> sentinel is now embedded in the PDF fixture
  2. Asymmetric ingestion coverage -> both /knowledge and /knowledge/text
     exercised across formats
  3. Weak retention check -> second purge on knowledge category with
     before/after assertions
  4. Agent-Luciel not re-fetched post-cascade -> now verified
  5. Above-scope negative silently skipped -> agent admin key minted in
     pillar 2 (before cascade), making pillar 8 unconditional
  6. No positive retrieval-scope test -> domain-bound chat verified to
     surface domain sentinel
  7. One-directional migration diff -> bidirectional + per-column
  8. Silent teardown -> new pillar 10 verifies zero step26-verify-* rows
     across all 15 tables
  9. Single-file layout -> subpackage, each pillar its own module
 10. No structured report -> --json-report artifact for 26b gate
"""

__version__ = "0.26.0"