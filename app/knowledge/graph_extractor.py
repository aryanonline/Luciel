"""Graph extraction + population pipeline (ARC 16).

Turns ingested knowledge (the chunks of a ``knowledge_sources`` row)
into graph nodes + edges. Runs as an additive stage after the Arc 11
parse→chunk→embed→persist flow — same source, same (admin_id,
instance_id) scope.

Two cleanly separated concerns:

  * **Extraction** (``EntityExtractor`` protocol) — reads chunk text and
    proposes typed entities + relationships. The production
    implementation is LLM-backed (an inference call); the contract is a
    narrow protocol so it is trivially stubbable in tests and the
    deterministic machinery around it is provable without a live model.
    This is the ONE step that needs an external model — isolated on
    purpose.

  * **Population** (``GraphIngestionService``) — deterministic, fully
    testable: entity resolution (dedup against existing nodes by the
    (admin, instance, type, label) key), source attribution (every node
    and edge carries the source_id — §3.2.2 trust contract), scope
    binding, lifecycle (re-ingest supersedes the source's old graph
    rows), and a never-raise envelope (graph extraction failure must
    never break ingestion of the vector chunks, which are the baseline).

Domain-agnostic (Locked Decision 5): the extractor returns free-text
``entity_type`` / ``relationship_type`` inferred from content + the
admin's business description. No vertical ontology is imposed here.

Correctness boundary: operates ONLY over the admin's ingested knowledge.
Never reads or writes live tool data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Extraction contract (the injectable, model-backed step)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractedEntity:
    entity_type: str
    entity_label: str
    attributes: dict | None = None


@dataclass(frozen=True)
class ExtractedRelation:
    src_label: str
    src_type: str
    dst_label: str
    dst_type: str
    relationship_type: str


@dataclass(frozen=True)
class ExtractionResult:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)


class EntityExtractor(Protocol):
    """Reads source text + business context, proposes entities/relations.

    The production implementation issues an LLM call with a
    domain-agnostic extraction prompt. Tests inject a deterministic
    stub. The protocol is intentionally tiny so the boundary between
    "needs a model" and "pure logic" is unambiguous.
    """

    def extract(
        self, *, text: str, business_description: str | None = None
    ) -> ExtractionResult:
        ...


# --------------------------------------------------------------------------
# LLM-backed extractor (the model step) — thin, swappable adapter.
# --------------------------------------------------------------------------
_EXTRACTION_SYSTEM_PROMPT = (
    "You extract a knowledge graph from a business's own documents. "
    "Identify the domain-specific ENTITIES and the RELATIONSHIPS between "
    "them. Do NOT impose a real-estate (or any single) ontology — infer "
    "entity and relationship types from THIS business's content and "
    "description. Examples of the SHAPE (not a fixed vocabulary): a "
    "realtor's doc yields Listing/Neighborhood/Feature nodes and IS_IN/"
    "HAS edges; a med-spa's yields Service/Practitioner/Condition nodes "
    "and TREATS/OFFERS edges. Return STRICT JSON only, no prose:\n"
    '{"entities":[{"entity_type":"<Type>","entity_label":"<Name>",'
    '"attributes":{<structured facts, numbers as numbers>}}],'
    '"relations":[{"src_label":"","src_type":"","dst_label":"",'
    '"dst_type":"","relationship_type":"<VERB>"}]}\n'
    "Only extract facts explicitly present in the text. Never invent."
)


class LLMEntityExtractor:
    """EntityExtractor backed by the platform LLM client (LLMBase).

    This is the ONE part of the pipeline that needs a live model. It is
    a thin adapter: build a prompt, call ``client.generate``, parse the
    JSON into ExtractionResult. Inject a StubLLMClient in tests; the
    deterministic population path is proven without a real model. Live
    validation against a real provider is a runbook step.

    Never raises: a malformed/empty model response yields an empty
    ExtractionResult (which the population service treats as a no-op),
    so graph extraction can never break vector ingestion.
    """

    def __init__(self, client, model: str | None = None) -> None:  # noqa: ANN001
        self._client = client
        self._model = model

    def extract(
        self, *, text: str, business_description: str | None = None
    ) -> ExtractionResult:
        from app.integrations.llm.base import LLMMessage, LLMRequest

        if not text or not text.strip():
            return ExtractionResult()
        user = text
        if business_description:
            user = f"[Business: {business_description}]\n\n{text}"
        try:
            resp = self._client.generate(
                LLMRequest(
                    messages=[
                        LLMMessage(role="system",
                                   content=_EXTRACTION_SYSTEM_PROMPT),
                        LLMMessage(role="user", content=user),
                    ],
                    model=self._model,
                    temperature=0.0,
                    max_tokens=2048,
                )
            )
            payload = _extract_json_object(resp.content)
            if payload is None:
                return ExtractionResult()
            entities = [
                ExtractedEntity(
                    entity_type=str(e["entity_type"]),
                    entity_label=str(e["entity_label"]),
                    attributes=e.get("attributes"),
                )
                for e in payload.get("entities", [])
                if e.get("entity_type") and e.get("entity_label")
            ]
            relations = [
                ExtractedRelation(
                    src_label=str(r["src_label"]),
                    src_type=str(r["src_type"]),
                    dst_label=str(r["dst_label"]),
                    dst_type=str(r["dst_type"]),
                    relationship_type=str(r["relationship_type"]),
                )
                for r in payload.get("relations", [])
                if r.get("src_label") and r.get("dst_label")
                and r.get("relationship_type")
            ]
            return ExtractionResult(entities=entities, relations=relations)
        except Exception as exc:  # noqa: BLE001 — never break ingestion
            logger.warning(
                "LLM entity extraction failed exc_class=%s — empty result",
                type(exc).__name__,
            )
            return ExtractionResult()


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort parse of a JSON object from a model response that may
    be wrapped in prose or markdown fences."""
    import json as _json

    if not raw:
        return None
    s = raw.strip()
    # strip ```json fences if present
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        if s.startswith("json"):
            s = s[4:]
    # find the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = _json.loads(s[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Population (deterministic, fully testable)
# --------------------------------------------------------------------------
class GraphIngestionService:
    """Persists extracted entities/relations into the graph store.

    Construct with a tenant-scoped session. ``populate_from_source``
    resolves entities (insert-or-get by the resolution key), wires
    edges, attributes everything to ``source_id``, and never raises —
    a failure is logged and leaves the (already-committed) vector chunks
    intact.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def populate_from_source(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        source_id: int,
        extraction: ExtractionResult,
        supersede_prior: bool = True,
    ) -> tuple[int, int]:
        """Insert nodes + edges for one source. Returns (n_nodes, n_edges)
        actually written. Never raises.

        ``supersede_prior``: on re-ingest of a source, stamp
        ``superseded_at`` on the source's existing graph rows before
        writing the new extraction — mirroring the chunk-versioning
        contract so the graph never carries stale duplicate facts.
        """
        try:
            if supersede_prior:
                self._supersede_source_rows(
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    source_id=source_id,
                )

            # Resolve / insert nodes, building a label→id map for edges.
            label_key_to_id: dict[tuple[str, str], int] = {}
            for ent in extraction.entities:
                node_id = self._resolve_or_insert_node(
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    source_id=source_id,
                    entity=ent,
                )
                label_key_to_id[(ent.entity_type, ent.entity_label)] = node_id

            # Insert edges, resolving endpoints (entities referenced in a
            # relation but not in the entity list are auto-resolved).
            n_edges = 0
            for rel in extraction.relations:
                src_id = label_key_to_id.get((rel.src_type, rel.src_label))
                if src_id is None:
                    src_id = self._resolve_or_insert_node(
                        admin_id=admin_id,
                        luciel_instance_id=luciel_instance_id,
                        source_id=source_id,
                        entity=ExtractedEntity(rel.src_type, rel.src_label),
                    )
                    label_key_to_id[(rel.src_type, rel.src_label)] = src_id
                dst_id = label_key_to_id.get((rel.dst_type, rel.dst_label))
                if dst_id is None:
                    dst_id = self._resolve_or_insert_node(
                        admin_id=admin_id,
                        luciel_instance_id=luciel_instance_id,
                        source_id=source_id,
                        entity=ExtractedEntity(rel.dst_type, rel.dst_label),
                    )
                    label_key_to_id[(rel.dst_type, rel.dst_label)] = dst_id

                if self._insert_edge(
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    source_id=source_id,
                    src_id=src_id,
                    dst_id=dst_id,
                    relationship_type=rel.relationship_type,
                ):
                    n_edges += 1

            self._db.commit()
            return len(label_key_to_id), n_edges
        except Exception as exc:  # noqa: BLE001 — never break ingestion
            logger.warning(
                "Graph population failed exc_class=%s source_id=%s "
                "instance_id=%s — vector chunks unaffected",
                type(exc).__name__, source_id, luciel_instance_id,
            )
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return (0, 0)

    # ---- internals ----
    def _supersede_source_rows(
        self, *, admin_id: str, luciel_instance_id: int, source_id: int
    ) -> None:
        for table in ("knowledge_graph_edges", "knowledge_graph_nodes"):
            self._db.execute(
                text(
                    f"""
                    UPDATE {table}
                       SET superseded_at = now()
                     WHERE admin_id = :a
                       AND luciel_instance_id = :i
                       AND source_id = :s
                       AND superseded_at IS NULL
                    """
                ),
                {"a": admin_id, "i": luciel_instance_id, "s": source_id},
            )

    def _resolve_or_insert_node(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        source_id: int,
        entity: ExtractedEntity,
    ) -> int:
        """Insert a node, or return the id of the existing active node
        with the same (admin, instance, type, label) resolution key.

        Uses ON CONFLICT on the unique resolution constraint so concurrent
        ingests don't fragment an entity. On conflict we refresh
        attributes + re-point source_id + clear any supersede stamp (the
        latest ingest "owns" the entity).
        """
        import json

        row = self._db.execute(
            text(
                """
                INSERT INTO knowledge_graph_nodes
                    (admin_id, luciel_instance_id, entity_type,
                     entity_label, attributes, source_id)
                VALUES (:a, :i, :etype, :label, :attrs ::jsonb, :s)
                ON CONFLICT (admin_id, luciel_instance_id,
                             entity_type, entity_label)
                DO UPDATE SET
                    attributes = COALESCE(
                        EXCLUDED.attributes,
                        knowledge_graph_nodes.attributes
                    ),
                    source_id = EXCLUDED.source_id,
                    superseded_at = NULL,
                    soft_deleted_at = NULL,
                    updated_at = now()
                RETURNING id
                """
            ),
            {
                "a": admin_id,
                "i": luciel_instance_id,
                "etype": entity.entity_type,
                "label": entity.entity_label,
                "attrs": json.dumps(entity.attributes)
                if entity.attributes is not None
                else None,
                "s": source_id,
            },
        ).scalar_one()
        return int(row)

    def _insert_edge(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        source_id: int,
        src_id: int,
        dst_id: int,
        relationship_type: str,
    ) -> bool:
        """Insert a directed edge; idempotent on the unique triple.
        Returns True if a row was inserted (False if it already existed).
        """
        res = self._db.execute(
            text(
                """
                INSERT INTO knowledge_graph_edges
                    (admin_id, luciel_instance_id, src_node_id,
                     dst_node_id, relationship_type, source_id)
                VALUES (:a, :i, :src, :dst, :rel, :s)
                ON CONFLICT (admin_id, luciel_instance_id, src_node_id,
                             dst_node_id, relationship_type)
                DO UPDATE SET
                    superseded_at = NULL,
                    soft_deleted_at = NULL,
                    source_id = EXCLUDED.source_id,
                    updated_at = now()
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "a": admin_id,
                "i": luciel_instance_id,
                "src": src_id,
                "dst": dst_id,
                "rel": relationship_type,
                "s": source_id,
            },
        ).scalar_one()
        return bool(res)
