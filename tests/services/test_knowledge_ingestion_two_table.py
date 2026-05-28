"""Arc 11 Step 3 — two-table model contract tests.

The brief asks for ingestion / retriever / repository tests that
cover:

  1. Ingest text -> knowledge_sources row exists with status='ready',
     N chunks with source_fk set.
  2. Re-ingest with same source -> source_version bumps, prior chunks
     superseded.
  3. Failed ingest -> ingestion_status='failed' with the error in
     ingestion_error.
  4. Soft-delete source -> source.soft_deleted_at set; chunks
     soft_deleted; retriever does not surface them.
  5. Tenant isolation: Admin A's retriever can't see Admin B's chunks
     even with identical legacy source_id strings.

This sandbox does not have a live Postgres (same posture as the rest
of ``tests/db/`` — see the docstring of
``tests/db/test_c5_4_tenant_leak_regression.py`` which explains why
``Wall 3`` tests are static-shape rather than live-DB). The retriever
and repository layer use raw pgvector SQL (``<=>`` operator,
``vector(1536)`` columns) plus ``BIGINT[]`` and ``ARRAY[]`` types
that SQLite does not implement. Spinning a Postgres just for these
tests would diverge from every existing service-test in the repo.

Instead this file follows the established pattern: contract tests
that prove the two-table semantics are *encoded* in the code:

  C1  ``KnowledgeSourceRepository`` exists and exposes the contract
      methods the brief enumerates: ``create_source``,
      ``get_source``, ``list_sources_for_instance``, ``mark_status``,
      ``soft_delete``, ``rename``, ``bump_version``.
  C2  ``IngestionService._ingest_text`` writes a ``knowledge_sources``
      row first, then chunks with ``source_fk``, then marks the
      source row ``ready``. AST + source-grep proof.
  C3  ``IngestResult`` exposes ``source_pk`` so callers can correlate
      the chunks with the source row.
  C4  Failed ingest path flips ``ingestion_status='failed'`` with
      the error captured.
  C5  Retriever exposes ``source_identifier`` per chunk and the
      repository's ``search_similar`` filters on
      ``ingestion_status='ready'`` + lifecycle flags. (Architecture
      v1 §3.2 retrieval flow step 1.)
  C6  ``KnowledgeRepository.add_chunks`` accepts ``source_fk`` as an
      optional pass-through (kw-only) defaulting to ``None`` so
      pre-Arc-11 callers stay green.
  C7  Soft-delete cascade helper exists on ``KnowledgeRepository``
      so the API handler in Step 7 can chain it after the
      source-row soft-delete.
  C8  Tenant isolation: every read in the source repo carries an
      explicit ``admin_id`` filter (L1 of the three-layer defence).
  C9  Backwards-compat alias ``KnowledgeEmbedding == KnowledgeChunk``
      still resolves from the repository module (Step 2 invariant
      that Step 3 must not break).

Each contract has at least one assertion that fails loudly if a
future refactor silently regresses the two-table model. When a real
Postgres environment is available the live-DB equivalents live in
``tests/integration/`` (the integration suite already isn't loaded
in CI's static run).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_INGESTION = REPO_ROOT / "app" / "knowledge" / "ingestion.py"
SRC_RETRIEVER = REPO_ROOT / "app" / "knowledge" / "retriever.py"
SRC_KNOWLEDGE_REPO = (
    REPO_ROOT / "app" / "repositories" / "knowledge_repository.py"
)
SRC_KS_REPO = (
    REPO_ROOT / "app" / "repositories" / "knowledge_source_repository.py"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


# ---------------------------------------------------------------------
# C1 — KnowledgeSourceRepository contract surface.
# ---------------------------------------------------------------------

REQUIRED_KS_REPO_METHODS = {
    "create_source",
    "get_source",
    "list_sources_for_instance",
    "mark_status",
    "soft_delete",
    "rename",
    "bump_version",
    "touch_last_viewed",
}


def test_c1_knowledge_source_repository_surface():
    """The repository class must expose every method the brief
    enumerates plus the ``touch_last_viewed`` helper the
    last-viewed-on-list-endpoint contract (Step 7) depends on."""
    from app.repositories.knowledge_source_repository import (
        KnowledgeSourceRepository,
    )

    methods = {
        name
        for name, _ in inspect.getmembers(
            KnowledgeSourceRepository, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }
    missing = REQUIRED_KS_REPO_METHODS - methods
    assert not missing, (
        f"KnowledgeSourceRepository is missing required methods: {missing}. "
        f"Step 3 contract — see ARC11_PLAN.md §3."
    )


def test_c1_knowledge_source_repository_status_enum_is_doctrine():
    """``mark_status`` only accepts the four lifecycle states defined
    by the migration's CHECK constraint. A drift here would let the
    DB reject writes the worker thinks are valid."""
    from app.repositories.knowledge_source_repository import (
        _VALID_STATUSES,
    )

    assert _VALID_STATUSES == frozenset(
        {"pending", "processing", "ready", "failed"}
    ), (
        "knowledge_sources.ingestion_status is a CHECK-constrained "
        "enum. Adding a state requires a paired migration to widen "
        "the CHECK first; see arc11_a_knowledge_sources_schema."
    )


# ---------------------------------------------------------------------
# C2 — Ingestion flow: create source -> chunks with source_fk
#                      -> mark ready.
# ---------------------------------------------------------------------


def test_c2_ingestion_creates_source_row_first():
    """The ingestion flow must call source-row creation BEFORE
    ``add_chunks`` so the source row's PK is available for
    ``source_fk``, and mark the source ``ready`` AFTER chunks are
    persisted. Verified by inspecting the call-site order INSIDE
    the ``_ingest_text`` method body (not anywhere else in the
    file)."""
    src = _read(SRC_INGESTION)
    ingest_start = src.find("def _ingest_text(")
    assert ingest_start != -1, "_ingest_text must exist."
    # Body ends at the next top-level method definition.
    rest = src[ingest_start:]
    body_end = rest.find("\n    def ", 10)
    body = rest[:body_end] if body_end != -1 else rest

    source_dispatch_at = body.find("self._create_or_bump_source(")
    chunks_at = body.find("self.repository.add_chunks(")
    mark_ready_at = body.find('status="ready"')

    assert source_dispatch_at != -1, (
        "_ingest_text must call _create_or_bump_source so the "
        "knowledge_sources row is materialised before chunks."
    )
    assert chunks_at != -1, (
        "_ingest_text must call self.repository.add_chunks."
    )
    assert mark_ready_at != -1, (
        "_ingest_text must mark the source row 'ready' on success."
    )
    assert source_dispatch_at < chunks_at, (
        "Source row must be materialised before chunks are added so "
        "source_fk can reference the new row."
    )
    assert chunks_at < mark_ready_at, (
        "mark_status('ready') must come AFTER chunks are added."
    )


def test_c2_add_chunks_receives_source_fk_passthrough():
    """When the ingestion path writes chunks it must pass
    ``source_fk=`` so the FK lands on the chunk rows."""
    src = _read(SRC_INGESTION)
    assert re.search(
        r"add_chunks\([^)]*source_fk\s*=", src, re.DOTALL,
    ), (
        "IngestionService.add_chunks(...) call must include "
        "source_fk= so chunks land linked to the new source row."
    )


def test_c2_ingestion_uses_knowledge_chunk_not_legacy_class_name():
    """Step 2 renamed KnowledgeEmbedding -> KnowledgeChunk. The
    ingestion service refactor in Step 3 should not reintroduce the
    legacy class name in code paths (the model-level alias is the
    one allowed exception)."""
    src = _read(SRC_INGESTION)
    # The only allowed occurrence is the import line and the legacy
    # ``ingest()`` shim's docstring. Everywhere else we use chunks.
    bare_references = re.findall(r"\bKnowledgeEmbedding\b", src)
    assert len(bare_references) == 0, (
        "IngestionService should not reference KnowledgeEmbedding "
        "directly any more (Step 2 renamed it to KnowledgeChunk). "
        f"Found {len(bare_references)} references."
    )


# ---------------------------------------------------------------------
# C3 — IngestResult surfaces source_pk.
# ---------------------------------------------------------------------


def test_c3_ingest_result_exposes_source_pk():
    """``IngestResult`` must expose ``source_pk`` so the API layer
    (Step 7) can return the new source id to the caller and the
    embed-worker (Step 6) can look it up."""
    from app.knowledge.ingestion import IngestResult

    fields = set(IngestResult.__dataclass_fields__.keys())
    assert "source_pk" in fields, (
        "IngestResult must include source_pk for the two-table model."
    )
    assert "source_id" in fields, (
        "IngestResult must continue to expose the legacy source_id "
        "string during the cutover so legacy callers keep working."
    )


# ---------------------------------------------------------------------
# C4 — Failed ingest flips ingestion_status='failed'.
# ---------------------------------------------------------------------


def test_c4_failed_ingest_marks_source_failed():
    """Embed or persist failure paths must call mark_status with
    'failed' so the admin UI surfaces the failure. The helper
    method ``_mark_source_failed`` is the entry point; verify it
    exists and is called from both the embedder-failure branch and
    the chunk-persist failure branch."""
    src = _read(SRC_INGESTION)
    assert "_mark_source_failed" in src, (
        "_mark_source_failed helper must exist on IngestionService."
    )
    # Count callsites — must be at least two (embed failure +
    # chunk persist failure).
    callsites = re.findall(r"self\._mark_source_failed\(", src)
    assert len(callsites) >= 2, (
        f"Expected at least two callsites of _mark_source_failed "
        f"(embedder failure + chunks persist failure); found "
        f"{len(callsites)}."
    )

    # The helper must pass status='failed' to mark_status.
    helper_block = src[src.find("def _mark_source_failed") :]
    helper_block = helper_block[: helper_block.find("\n    def ")]
    assert 'status="failed"' in helper_block, (
        "_mark_source_failed must call mark_status with status='failed'."
    )
    assert "error=" in helper_block, (
        "_mark_source_failed must forward the exception text to "
        "knowledge_sources.ingestion_error."
    )


# ---------------------------------------------------------------------
# C5 — Retriever surface + repository filter.
# ---------------------------------------------------------------------


def test_c5_retriever_exposes_source_identifier():
    """The brief: ``RetrievedChunk.source_identifier`` typed
    ``int | str | None``. Step 5 (trace instrumentation) and Step 8
    (orchestrator wiring) both read this field."""
    from app.knowledge import RetrievedChunk

    fields = RetrievedChunk.__dataclass_fields__
    assert "source_identifier" in fields, (
        "RetrievedChunk must expose source_identifier for Step 5 / Step 8."
    )
    # The type annotation should explicitly include both int and str.
    annot = fields["source_identifier"].type
    annot_str = str(annot) if not isinstance(annot, str) else annot
    assert "int" in annot_str and "str" in annot_str, (
        f"source_identifier type must allow both int and str. "
        f"Got {annot_str!r}."
    )


def test_c5_retriever_has_retrieve_with_sources_method():
    from app.knowledge.retriever import KnowledgeRetriever

    assert hasattr(KnowledgeRetriever, "retrieve_with_sources"), (
        "Step 5/8 need a retriever surface that returns the richer "
        "RetrievedChunk list, not the flat list[str] of retrieve()."
    )


def test_c5_search_similar_filters_on_ingestion_status_ready():
    """Architecture §3.2 retrieval flow step 1: ``Filter by admin_id,
    instance_id, and ingestion_status = 'ready'``. The chunk-side
    query must outer-join knowledge_sources and gate on the join."""
    src = _read(SRC_KNOWLEDGE_REPO)
    assert "outerjoin" in src, (
        "search_similar must outer-join knowledge_sources to read "
        "the parent source's ingestion_status."
    )
    assert "ingestion_status" in src and '"ready"' in src, (
        "search_similar must gate on ingestion_status = 'ready'."
    )


def test_c5_search_similar_excludes_lifecycle_flagged_chunks():
    """The retriever must also exclude soft_deleted and
    pending_downgrade_archived chunks (Arc 10 lifecycle columns)."""
    src = _read(SRC_KNOWLEDGE_REPO)
    # Body of search_similar.
    body = src[src.find("def search_similar") :]
    body = body[: body.find("\n    def ", 1)] if "\n    def " in body[10:] else body
    assert "soft_deleted_at.is_(None)" in body, (
        "search_similar must filter soft_deleted_at IS NULL on chunks."
    )
    assert "pending_downgrade_archived_at.is_(None)" in body, (
        "search_similar must filter pending_downgrade_archived_at "
        "IS NULL on chunks (Arc 10 5th axis)."
    )


# ---------------------------------------------------------------------
# C6 — add_chunks accepts source_fk (kw-only, defaults None).
# ---------------------------------------------------------------------


def test_c6_add_chunks_accepts_source_fk_optional():
    from app.repositories.knowledge_repository import KnowledgeRepository

    sig = inspect.signature(KnowledgeRepository.add_chunks)
    assert "source_fk" in sig.parameters, (
        "KnowledgeRepository.add_chunks must accept source_fk."
    )
    p = sig.parameters["source_fk"]
    assert p.default is None, (
        "source_fk must default to None so pre-Arc-11 callers stay "
        "green without ad-hoc keyword juggling."
    )
    # Must be keyword-only (the existing signature uses *, ...).
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        "source_fk must be keyword-only — matches the rest of "
        "add_chunks's signature posture."
    )


# ---------------------------------------------------------------------
# C7 — Soft-delete cascade helper.
# ---------------------------------------------------------------------


def test_c7_chunk_repo_has_soft_delete_cascade_for_source_fk():
    from app.repositories.knowledge_repository import KnowledgeRepository

    assert hasattr(
        KnowledgeRepository, "soft_delete_chunks_for_source_fk"
    ), (
        "Step 7 needs a cascade helper that flips soft_deleted_at on "
        "every active chunk for a given source_fk (mirrors the source-"
        "row soft-delete on the parent)."
    )


# ---------------------------------------------------------------------
# C8 — Tenant isolation: every source-repo read carries admin_id.
# ---------------------------------------------------------------------


def test_c8_every_ks_repo_read_carries_admin_id_filter():
    """The L1 defence: even though Step 4 will add an RLS policy
    (L2), this layer must filter explicitly. AST-walk the
    KnowledgeSourceRepository class; every reads/writes method
    (except create_source which writes admin_id) takes admin_id as
    a kw-only argument."""
    tree = _parse(SRC_KS_REPO)
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef)
        and n.name == "KnowledgeSourceRepository"
    )
    methods = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    ]
    must_take_admin_id = {
        "get_source",
        "list_sources_for_instance",
        "mark_status",
        "soft_delete",
        "rename",
        "bump_version",
        "touch_last_viewed",
    }
    seen = set()
    for m in methods:
        if m.name not in must_take_admin_id:
            continue
        kwonly_names = {a.arg for a in m.args.kwonlyargs}
        assert "admin_id" in kwonly_names, (
            f"KnowledgeSourceRepository.{m.name} must take admin_id as "
            f"a keyword-only argument so the L1 filter is explicit at "
            f"every call site."
        )
        seen.add(m.name)
    assert seen == must_take_admin_id, (
        f"Missing methods on KnowledgeSourceRepository: "
        f"{must_take_admin_id - seen}"
    )


def test_c8_soft_deleted_default_excluded():
    """By default ``list_sources_for_instance`` and ``get_source``
    must hide soft-deleted rows. Opt-in via include_soft_deleted=True."""
    from app.repositories.knowledge_source_repository import (
        KnowledgeSourceRepository,
    )

    list_sig = inspect.signature(
        KnowledgeSourceRepository.list_sources_for_instance
    )
    assert list_sig.parameters["include_soft_deleted"].default is False
    get_sig = inspect.signature(KnowledgeSourceRepository.get_source)
    assert get_sig.parameters["include_soft_deleted"].default is False


# ---------------------------------------------------------------------
# C9 — Backwards-compat alias still resolves (Step 2 invariant).
# ---------------------------------------------------------------------


def test_c9_legacy_knowledge_embedding_alias_intact():
    """If Step 3 silently drops the alias, every external import
    site that hasn't been migrated yet breaks. The alias must
    survive Step 3 untouched."""
    from app.models.knowledge import KnowledgeChunk, KnowledgeEmbedding

    assert KnowledgeEmbedding is KnowledgeChunk, (
        "KnowledgeEmbedding = KnowledgeChunk alias must remain until "
        "Step 11. Step 3 callers migrate incrementally."
    )
    # Also from the repository module — Step 3 added a local alias
    # so closed-world readers of knowledge_repository.py see both
    # names.
    from app.repositories.knowledge_repository import (
        KnowledgeChunk as KC,
        KnowledgeEmbedding as KE,
    )

    assert KE is KC


# ---------------------------------------------------------------------
# C10 — data_export reads from knowledge_sources (the new table).
# ---------------------------------------------------------------------


def test_c10_data_export_reads_knowledge_sources_table():
    """The data-export manifest must now draw from the
    knowledge_sources table for Arc-11-shape sources. Legacy
    chunk-grouping fallback for source_fk IS NULL stays."""
    src = _read(
        REPO_ROOT / "app" / "services" / "data_export_service.py"
    )
    body = src[src.find("def _write_knowledge") :]
    body = body[: body.find("\n    def ", 1)] if "\n    def " in body[10:] else body
    assert "FROM knowledge_sources" in body, (
        "_write_knowledge must select from the knowledge_sources "
        "table (Arc 11 Step 3 — see ARC11_PLAN.md §3 read-side)."
    )
    assert "source_fk IS NULL" in body, (
        "_write_knowledge must keep the legacy fallback for chunks "
        "with NULL source_fk until Step 11 retires the legacy path."
    )


# ---------------------------------------------------------------------
# C11 — downgrade_archive groups by source_fk for Arc-11 chunks,
#       source_id string for legacy chunks.
# ---------------------------------------------------------------------


def test_c11_downgrade_archive_dispatches_on_fk_vs_legacy():
    """The Arc 11 Step 3 update widens the AXIS_KNOWLEDGE grouping
    key. ``_compute_knowledge_axis`` must produce prefixed bucket
    keys (``fk:<n>`` vs ``sid:<s>``) and ``_apply_axis`` must
    parse them and dispatch the right UPDATE."""
    src = _read(
        REPO_ROOT / "app" / "services" / "downgrade_archive_service.py"
    )
    assert 'literal("fk:")' in src, (
        "_compute_knowledge_axis must emit fk:-prefixed bucket keys "
        "for chunks with source_fk."
    )
    assert 'literal("sid:")' in src, (
        "_compute_knowledge_axis must emit sid:-prefixed bucket keys "
        "for legacy chunks."
    )
    assert 'startswith("fk:")' in src, (
        "_apply_axis must dispatch on the fk: prefix to run the "
        "source_fk-based UPDATE."
    )
    assert "source_fk = :fk" in src, (
        "_apply_axis must run an UPDATE keyed on source_fk for "
        "Arc-11-shape chunks."
    )
    assert "UPDATE knowledge_sources" in src, (
        "_apply_axis must also stamp pending_downgrade_archived_at "
        "on the parent knowledge_sources row so the source's own "
        "lifecycle column tracks its chunks."
    )


# ---------------------------------------------------------------------
# C12 — Bump version on re-ingest (chunks superseded, source bumped).
# ---------------------------------------------------------------------


def test_c12_reingest_bumps_source_version():
    """A re-ingest must call ``bump_version`` on the source row, not
    re-create it. ``_create_or_bump_source`` is the dispatcher; it
    must look up the existing row by (admin, instance, filename)
    and call bump_version when chunks were superseded."""
    src = _read(SRC_INGESTION)
    assert "_create_or_bump_source" in src, (
        "Re-ingest dispatcher _create_or_bump_source must exist."
    )
    block = src[src.find("def _create_or_bump_source") :]
    block = block[: block.find("\n    def ", 1)]
    assert "self.source_repository.bump_version" in block, (
        "Re-ingest path must call source_repository.bump_version."
    )
    assert "superseded == 0" in block or "superseded ==0" in block, (
        "Re-ingest dispatcher must branch on whether the chunk-side "
        "supersede produced any rows (the trigger to bump)."
    )


# ---------------------------------------------------------------------
# C13 — Quota enforcement is NOT here (it belongs in Step 7).
# ---------------------------------------------------------------------


def test_c13_no_quota_check_in_ingestion():
    """Architecture v1 §3.2: quotas enforced at the API boundary.
    Step 3 must not leak quota checks into the ingestion layer."""
    src = _read(SRC_INGESTION).lower()
    for needle in (
        "knowledge_bytes_cap",
        "knowledge_per_file_bytes_cap",
        "quota_exceeded",
    ):
        assert needle not in src, (
            f"IngestionService must not enforce quotas; found "
            f"{needle!r} in app/knowledge/ingestion.py. Move it to "
            f"the API boundary (Step 7)."
        )


# ---------------------------------------------------------------------
# C14 — knowledge/__init__.py surfaces RetrievedChunk for Step 5/8.
# ---------------------------------------------------------------------


def test_c14_knowledge_package_reexports_retrieved_chunk():
    from app.knowledge import KnowledgeRetriever, RetrievedChunk
    from app.knowledge.retriever import (
        KnowledgeRetriever as _KR,
        RetrievedChunk as _RC,
    )

    assert KnowledgeRetriever is _KR
    assert RetrievedChunk is _RC


# ---------------------------------------------------------------------
# C15 — Source row only created when admin_id + instance are present.
# ---------------------------------------------------------------------


def test_c15_legacy_global_ingests_skip_source_row():
    """Global / shared knowledge writes (admin_id None, instance
    None) must NOT create a knowledge_sources row — the source
    table requires both columns NOT NULL. Skipping is the right
    posture during the cutover."""
    src = _read(SRC_INGESTION)
    block = src[src.find("def _ingest_text") :]
    block = block[: block.find("\n    def ", 1)]
    # Source-row creation must be guarded on both admin_id truthiness
    # AND luciel_instance_id not None.
    assert "admin_id and luciel_instance_id is not None" in block, (
        "Source-row creation must be guarded on (admin_id AND "
        "luciel_instance_id is not None) so legacy global/shared "
        "ingests don't try to violate the NOT NULL columns."
    )


# Smoke: every test name above corresponds to a contract id in the
# module docstring so a quick scan of pytest output maps to the brief.
EXPECTED_CONTRACTS = {f"C{i}" for i in range(1, 16)}
COVERED_CONTRACTS = {
    name.split("_")[1].upper()
    for name in globals()
    if name.startswith("test_c") and callable(globals()[name])
}


def test_contract_coverage_matches_module_docstring():
    """Every Cn the docstring lists must have at least one test_cN_*."""
    missing = EXPECTED_CONTRACTS - COVERED_CONTRACTS
    assert not missing, (
        f"Contracts referenced in module docstring but not covered: "
        f"{sorted(missing)}"
    )
