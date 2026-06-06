"""Two-table knowledge model contract tests — post-Cleanup-B.

The brief (Arc 11 Step 3, refined by Cleanup B) covers:

  1. Ingest text -> knowledge_sources row exists with status='ready',
     N chunks with the INTEGER ``source_id`` FK set.
  2. Re-ingest is handled by Step 7's PATCH route (bump_version at the
     source-row level); IngestionService itself no longer branches on
     replace_existing.
  3. Failed ingest -> ingestion_status='failed' with the error in
     ingestion_error.
  4. Soft-delete source -> source.soft_deleted_at set; chunks
     soft_deleted via the cascade helper.
  5. Tenant isolation: every source-repo read filters on admin_id.

This sandbox does not have a live Postgres (same posture as the rest
of ``tests/db/`` — Wall 3 tests are static-shape rather than live-DB
because the retriever and repository use raw pgvector SQL (``<=>``
operator, ``vector(1536)`` columns) plus ``BIGINT[]`` types that
SQLite does not implement).

These are contract tests proving the post-Cleanup-B two-table
semantics are encoded in the code:

  C1  KnowledgeSourceRepository exposes the contract methods.
  C2  IngestionService._ingest_text creates the source row first,
      then chunks with the INTEGER ``source_id`` FK, then marks
      ready.
  C3  IngestResult exposes ``source_id`` (INTEGER FK to
      knowledge_sources.id).
  C4  Failed ingest path flips ingestion_status='failed' with the
      error captured.
  C5  Retriever exposes ``source_identifier: int`` and
      search_similar gates on ingestion_status='ready' +
      source-side lifecycle.
  C6  KnowledgeRepository.add_chunks accepts ``source_id: int``
      as a mandatory kw-only FK.
  C7  Soft-delete cascade helper exists as
      ``soft_delete_chunks_for_source_id``.
  C8  Tenant isolation: every read carries admin_id.
  C9  KnowledgeEmbedding alias is REMOVED (Cleanup B closeout).
  C10 data_export reads from knowledge_sources; legacy fallback is
      gone.
  C11 downgrade_archive groups by the INTEGER source_id FK
      directly; prefixed bucket keys are gone.
  C12 No legacy ``ingest()`` shim; no ``_create_or_bump_source``
      helper; no ``replace_existing`` branch.
  C13 Quota enforcement is NOT here (it belongs in Step 7).
  C14 knowledge/__init__.py surfaces RetrievedChunk.
  C15 admin_id AND luciel_instance_id are MANDATORY in
      _ingest_text (post-Cleanup-B no legacy global/shared path).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path


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
        f"KnowledgeSourceRepository is missing required methods: {missing}."
    )


def test_c1_knowledge_source_repository_status_enum_is_doctrine():
    from app.repositories.knowledge_source_repository import (
        _VALID_STATUSES,
    )

    assert _VALID_STATUSES == frozenset(
        {"pending", "processing", "ready", "failed"}
    )


# ---------------------------------------------------------------------
# C2 — Ingestion flow: create source -> chunks with source_id (int FK)
#                      -> mark ready.
# ---------------------------------------------------------------------


def test_c2_ingestion_creates_source_row_first():
    """The ingestion flow must create the source row BEFORE
    ``add_chunks`` so the source row's PK is available for
    ``source_id`` (int FK), and mark the source ``ready`` AFTER
    chunks are persisted."""
    src = _read(SRC_INGESTION)
    ingest_start = src.find("def _ingest_text(")
    assert ingest_start != -1
    rest = src[ingest_start:]
    body_end = rest.find("\n    def ", 10)
    body = rest[:body_end] if body_end != -1 else rest

    source_create_at = body.find("self.source_repository.create_source(")
    chunks_at = body.find("self.repository.add_chunks(")
    mark_ready_at = body.find('status="ready"')

    assert source_create_at != -1, (
        "_ingest_text must call source_repository.create_source so the "
        "knowledge_sources row is materialised before chunks."
    )
    assert chunks_at != -1
    assert mark_ready_at != -1
    assert source_create_at < chunks_at, (
        "Source row must be materialised before chunks are added so "
        "source_id can reference the new row."
    )
    assert chunks_at < mark_ready_at, (
        "mark_status('ready') must come AFTER chunks are added."
    )


def test_c2_add_chunks_receives_source_id_int_fk():
    """The ingestion path must pass ``source_id=`` (int FK)
    when writing chunks."""
    src = _read(SRC_INGESTION)
    assert re.search(
        r"add_chunks\([^)]*source_id\s*=", src, re.DOTALL,
    ), (
        "IngestionService.add_chunks(...) call must include "
        "source_id= so chunks land linked to the new source row."
    )


def test_c2_ingestion_uses_knowledge_chunk_not_legacy_class_name():
    """Cleanup B removed the KnowledgeEmbedding alias entirely."""
    src = _read(SRC_INGESTION)
    bare_references = re.findall(r"\bKnowledgeEmbedding\b", src)
    assert len(bare_references) == 0, (
        f"IngestionService must not reference KnowledgeEmbedding "
        f"(removed in Cleanup B). Found {len(bare_references)} references."
    )


# ---------------------------------------------------------------------
# C3 — IngestResult surfaces source_id (INTEGER FK).
# ---------------------------------------------------------------------


def test_c3_ingest_result_exposes_source_id_int_fk():
    """``IngestResult.source_id`` is the INTEGER FK to
    knowledge_sources.id (post-Cleanup-B). The legacy ``source_pk``
    + stringy ``source_id`` pair collapsed to this one field."""
    from app.knowledge.ingestion import IngestResult

    fields = IngestResult.__dataclass_fields__
    assert "source_id" in fields, (
        "IngestResult must expose source_id (the INTEGER FK)."
    )
    annot = fields["source_id"].type
    annot_str = str(annot) if not isinstance(annot, str) else annot
    assert "int" in annot_str, (
        f"IngestResult.source_id must be typed int. Got {annot_str!r}."
    )
    # ``source_pk`` was the pre-Cleanup-B name; gone post-Cleanup-B.
    assert "source_pk" not in fields, (
        "IngestResult.source_pk was the pre-Cleanup-B name; removed "
        "post-Cleanup-B (source_id is the int FK directly)."
    )


# ---------------------------------------------------------------------
# C4 — Failed ingest flips ingestion_status='failed'.
# ---------------------------------------------------------------------


def test_c4_failed_ingest_marks_source_failed():
    src = _read(SRC_INGESTION)
    assert "_mark_source_failed" in src
    callsites = re.findall(r"self\._mark_source_failed\(", src)
    assert len(callsites) >= 2, (
        f"Expected at least two callsites of _mark_source_failed "
        f"(embedder failure + chunks persist failure); found "
        f"{len(callsites)}."
    )

    helper_block = src[src.find("def _mark_source_failed") :]
    helper_block = helper_block[: helper_block.find("\n    def ")]
    assert 'status="failed"' in helper_block
    assert "error=" in helper_block


# ---------------------------------------------------------------------
# C5 — Retriever surface + repository filter.
# ---------------------------------------------------------------------


def test_c5_retriever_exposes_source_identifier_as_int():
    """Post-Cleanup-B: ``RetrievedChunk.source_identifier: int``.
    The pre-Cleanup-B ``int | str | None`` union collapsed because
    every chunk now has a non-NULL INTEGER source_id FK."""
    from app.knowledge import RetrievedChunk

    fields = RetrievedChunk.__dataclass_fields__
    assert "source_identifier" in fields
    annot = fields["source_identifier"].type
    annot_str = str(annot) if not isinstance(annot, str) else annot
    assert "int" in annot_str
    assert "str" not in annot_str, (
        f"source_identifier must be int-only post-Cleanup-B (no more "
        f"legacy str fallback). Got {annot_str!r}."
    )
    assert "None" not in annot_str, (
        f"source_identifier must not allow None post-Cleanup-B "
        f"(source_id FK is NOT NULL). Got {annot_str!r}."
    )


def test_c5_retriever_has_retrieve_with_sources_method():
    from app.runtime.knowledge_retrieval import KnowledgeRetriever

    assert hasattr(KnowledgeRetriever, "retrieve_with_sources")


def test_c5_search_similar_inner_joins_knowledge_sources():
    """Post-Cleanup-B: source_id is NOT NULL so the join becomes
    INNER (was LEFT OUTER pre-Cleanup-B to tolerate legacy chunks)."""
    src = _read(SRC_KNOWLEDGE_REPO)
    assert ".join(ks," in src, (
        "search_similar must INNER JOIN knowledge_sources via the "
        "alias ks (was outerjoin pre-Cleanup-B)."
    )
    assert "outerjoin" not in src, (
        "search_similar must NOT outerjoin knowledge_sources "
        "post-Cleanup-B; source_id is NOT NULL."
    )
    assert "ingestion_status" in src and '"ready"' in src


def test_c5_search_similar_excludes_lifecycle_flagged_chunks():
    src = _read(SRC_KNOWLEDGE_REPO)
    body = src[src.find("def search_similar") :]
    body = body[: body.find("\n    def ", 1)] if "\n    def " in body[10:] else body
    assert "soft_deleted_at.is_(None)" in body
    assert "pending_downgrade_archived_at.is_(None)" in body


# ---------------------------------------------------------------------
# C6 — add_chunks accepts source_id (kw-only, int, mandatory).
# ---------------------------------------------------------------------


def test_c6_add_chunks_accepts_source_id_mandatory_int():
    from app.repositories.knowledge_repository import KnowledgeRepository

    sig = inspect.signature(KnowledgeRepository.add_chunks)
    assert "source_id" in sig.parameters, (
        "KnowledgeRepository.add_chunks must accept source_id."
    )
    p = sig.parameters["source_id"]
    assert p.default is inspect.Parameter.empty, (
        "source_id must be mandatory post-Cleanup-B (FK is NOT NULL)."
    )
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    # ``source_fk`` was the pre-Cleanup-B kwarg name; gone.
    assert "source_fk" not in sig.parameters, (
        "source_fk was renamed to source_id in Cleanup B."
    )


# ---------------------------------------------------------------------
# C7 — Soft-delete cascade helper renamed for new column name.
# ---------------------------------------------------------------------


def test_c7_chunk_repo_has_soft_delete_cascade_for_source_id():
    from app.repositories.knowledge_repository import KnowledgeRepository

    assert hasattr(
        KnowledgeRepository, "soft_delete_chunks_for_source_id"
    ), (
        "Step 7 needs a cascade helper that flips soft_deleted_at on "
        "every active chunk for a given source_id (renamed from "
        "soft_delete_chunks_for_source_fk in Cleanup B)."
    )
    assert not hasattr(
        KnowledgeRepository, "soft_delete_chunks_for_source_fk"
    ), (
        "soft_delete_chunks_for_source_fk was renamed in Cleanup B; "
        "the old name must not still resolve."
    )


# ---------------------------------------------------------------------
# C8 — Tenant isolation: every source-repo read carries admin_id.
# ---------------------------------------------------------------------


def test_c8_every_ks_repo_read_carries_admin_id_filter():
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
            f"a keyword-only argument."
        )
        seen.add(m.name)
    assert seen == must_take_admin_id, (
        f"Missing methods on KnowledgeSourceRepository: "
        f"{must_take_admin_id - seen}"
    )


def test_c8_soft_deleted_default_excluded():
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
# C9 — KnowledgeEmbedding alias REMOVED in Cleanup B.
# ---------------------------------------------------------------------


def test_c9_legacy_knowledge_embedding_alias_removed():
    """Cleanup B drops the ``KnowledgeEmbedding = KnowledgeChunk``
    alias entirely. Any importer that still references the old name
    must be migrated."""
    import app.models.knowledge as km

    assert not hasattr(km, "KnowledgeEmbedding"), (
        "KnowledgeEmbedding alias was removed in Cleanup B. "
        "app.models.knowledge must not expose it."
    )


# ---------------------------------------------------------------------
# C10 — data_export reads from knowledge_sources; legacy fallback gone.
# ---------------------------------------------------------------------


def test_c10_data_export_reads_knowledge_sources_table():
    """Post-Cleanup-B: manifest entries are drawn directly from
    ``knowledge_sources``. The legacy chunk-grouping fallback
    (``source_fk IS NULL``) is gone."""
    src = _read(
        REPO_ROOT / "app" / "services" / "data_export_service.py"
    )
    body = src[src.find("def _write_knowledge") :]
    body = body[: body.find("\n    def ", 1)] if "\n    def " in body[10:] else body
    assert "FROM knowledge_sources" in body, (
        "_write_knowledge must select from knowledge_sources."
    )
    assert "source_fk IS NULL" not in body, (
        "Legacy fallback (source_fk IS NULL) must be removed in "
        "Cleanup B; chunks always have an INTEGER source_id FK now."
    )


# ---------------------------------------------------------------------
# C11 — downgrade_archive groups by the INTEGER source_id FK directly.
# ---------------------------------------------------------------------


def test_c11_downgrade_archive_groups_by_source_id_int():
    """Post-Cleanup-B: ``_compute_knowledge_axis`` groups directly
    by the INTEGER ``source_id`` FK. The pre-Cleanup-B prefixed
    bucket keys (``fk:<n>`` / ``sid:<s>``) and the prefix-dispatch
    in ``_apply_axis`` are gone."""
    src = _read(
        REPO_ROOT / "app" / "services" / "downgrade_archive_service.py"
    )
    assert 'literal("fk:")' not in src, (
        "Prefixed bucket keys removed in Cleanup B."
    )
    assert 'literal("sid:")' not in src, (
        "Prefixed bucket keys removed in Cleanup B."
    )
    assert 'startswith("fk:")' not in src, (
        "Prefix dispatch removed in Cleanup B."
    )
    assert "source_id = :sid" in src, (
        "_apply_axis must UPDATE knowledge_chunks keyed on the new "
        "INTEGER source_id FK."
    )
    assert "UPDATE knowledge_sources" in src, (
        "_apply_axis must mirror-stamp pending_downgrade_archived_at "
        "on the parent knowledge_sources row."
    )


# ---------------------------------------------------------------------
# C12 — Legacy shim + re-ingest dispatcher gone.
# ---------------------------------------------------------------------


def test_c12_legacy_ingest_shim_and_dispatcher_gone():
    """Cleanup B removed the legacy ``ingest()`` shim (its only caller
    was the deleted ``POST /admin/knowledge/ingest`` route) and the
    ``_create_or_bump_source`` dispatcher (versioning lives at the
    source-row level via Step 7's PATCH route)."""
    src = _read(SRC_INGESTION)
    assert "_create_or_bump_source" not in src, (
        "_create_or_bump_source dispatcher removed in Cleanup B."
    )
    assert "replace_existing" not in src, (
        "replace_existing parameter removed in Cleanup B; re-ingest "
        "is handled at the API layer via bump_version."
    )
    # The one-arg legacy ``def ingest(`` shim must be gone too — only
    # the public ``ingest_text`` / ``ingest_file`` entry points remain.
    assert re.search(r"\n    def ingest\(", src) is None, (
        "Legacy IngestionService.ingest() shim removed in Cleanup B."
    )


# ---------------------------------------------------------------------
# C13 — Quota enforcement is NOT here (it belongs in Step 7).
# ---------------------------------------------------------------------


def test_c13_no_quota_check_in_ingestion():
    src = _read(SRC_INGESTION).lower()
    for needle in (
        "knowledge_bytes_cap",
        "knowledge_per_file_bytes_cap",
        "quota_exceeded",
    ):
        assert needle not in src, (
            f"IngestionService must not enforce quotas; found "
            f"{needle!r} in app/knowledge/ingestion.py."
        )


# ---------------------------------------------------------------------
# C14 — knowledge/__init__.py surfaces RetrievedChunk for Step 5/8.
# ---------------------------------------------------------------------


def test_c14_knowledge_package_reexports_retrieved_chunk():
    from app.knowledge import KnowledgeRetriever, RetrievedChunk
    from app.runtime.knowledge_retrieval import (
        KnowledgeRetriever as _KR,
        RetrievedChunk as _RC,
    )

    assert KnowledgeRetriever is _KR
    assert RetrievedChunk is _RC


# ---------------------------------------------------------------------
# C15 — admin_id AND luciel_instance_id are MANDATORY.
# ---------------------------------------------------------------------


def test_c15_ingest_requires_admin_id_and_instance():
    """Post-Cleanup-B the source row's NOT NULL columns make both
    mandatory. The legacy "global/shared knowledge" path (admin_id
    None, instance None, write chunks with NULL FK) is gone."""
    src = _read(SRC_INGESTION)
    block = src[src.find("def _ingest_text") :]
    block = block[: block.find("\n    def ", 1)]
    assert "admin_id is required" in block, (
        "_ingest_text must raise IngestionError when admin_id is "
        "missing — the source-row contract requires it post-Cleanup-B."
    )
    assert "luciel_instance_id is required" in block, (
        "_ingest_text must raise IngestionError when "
        "luciel_instance_id is missing."
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
    missing = EXPECTED_CONTRACTS - COVERED_CONTRACTS
    assert not missing, (
        f"Contracts referenced in module docstring but not covered: "
        f"{sorted(missing)}"
    )
