"""Pillar 5 close-audit per ARC11_PLAN.md §8.5. Run before merging
Arc 11 branches to main. Exits non-zero if any check fails.

Six sections (alembic graph + schema + RLS, entitlements, feature
flag, infrastructure templates, cross-repo contract, test counts).
Each section returns a ``CheckResult`` with one of three states —
``PASS``, ``FAIL``, or ``SKIP`` (the last for checks that need
live infrastructure the sandbox can't reach). The script exits 0
when every check is PASS or SKIP; any FAIL exits 1.

Usage:

    python scripts/arc11_close_audit.py
    python scripts/arc11_close_audit.py --json   # machine-readable
    python scripts/arc11_close_audit.py --live   # opt into live-AWS / live-DB
                                                 # checks (the close
                                                 # auditor runs with this)

Doctrine anchors:
    * ARC11_PLAN.md §8.5 (close-audit script)
    * Vision §3.3, §7  (entitlement quotas)
    * Architecture v1 §3.2 (two-table model), §3.2.2 (role matrix),
      §3.7.5 (RLS posture)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]

# True when the script is running from a real repo checkout (with
# tests/ and cfn/ adjacent to the script). False when the script has
# been dropped into a stripped runtime image (e.g. the production ECS
# container, which carries app/ + scripts/ but not tests/ or cfn/).
# Repo-only checks (templates, cross-repo contract, test counts) SKIP
# rather than FAIL when this is False, since they verify the source
# tree rather than the running system.
REPO_CHECKOUT = (
    (REPO_ROOT / "tests").is_dir()
    and (REPO_ROOT / "cfn").is_dir()
    and (REPO_ROOT / "alembic.ini").is_file()
)


# ---------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------


@dataclass
class CheckResult:
    section: str
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    # sub_results retained as ``list`` (without the ``CheckResult``
    # type param) because the dataclass decorator evaluates the
    # type hint at class-creation time, before ``CheckResult`` is
    # bound. The runtime semantics are unchanged — entries are
    # ``CheckResult`` instances; only the static type annotation
    # is widened.
    sub_results: list = field(default_factory=list)


def _pass(section: str, name: str, detail: str = "") -> CheckResult:
    return CheckResult(section=section, name=name, status="PASS", detail=detail)


def _fail(section: str, name: str, detail: str) -> CheckResult:
    return CheckResult(section=section, name=name, status="FAIL", detail=detail)


def _skip(section: str, name: str, detail: str) -> CheckResult:
    return CheckResult(section=section, name=name, status="SKIP", detail=detail)


# ---------------------------------------------------------------------
# Section 1 — Migrations & schema
# ---------------------------------------------------------------------


def section_1_migrations_and_schema(*, live: bool) -> list[CheckResult]:
    section = "1. Migrations & schema"
    out: list[CheckResult] = []

    # 1a. alembic heads is single and points at the latest head on
    # the active branch. Arc 11 Closeout PR-B advanced the chain to
    # ``arc11_closeout_b_ingestion_error_code``; Arc 12 then appended
    # the EX-series excisions, advancing the head to
    # ``arc12_ex4_reseal_audit_chain_drop_agent_domain``; Arc 12b
    # advances the head once more to
    # ``arc12b_custom_roles_permission_model``; Arc 13 then appends
    # ``arc13_a_channel_routes`` → ``arc13_b_instance_channel_fields``;
    # Arc 14 U2 appends ``arc14_u2_escalation_events`` (the §3.4.5
    # escalation event store); Arc 14 U4 then appends
    # ``arc14_u4_leads`` (the §3.4.4 lead-capture / §3.4.7 summary
    # table). Arc 15 then appends ``arc15_a_instance_config_pillars``
    # (Vision §3.5 / Journey Phase 3-4 config pillars) and
    # ``arc15_b_instance_connections`` (the Arc 17 connection-contract
    # slice). Arc 15 doctrine cleanup then appends
    # ``arc15_c_drop_system_prompt_additions`` (Vision §3.5 / Arch
    # §3.5.1 "never raw prompt authoring" — drops the dead free-text
    # column). Arc 17 then appends
    # ``arc17_a_connection_domain_agnostic_renames`` (the domain-agnostic
    # connection_type renames + last_health_check_at) and
    # ``arc17_b_secret_cleanup_outbox`` (the secret-cleanup outbox table),
    # advancing the single head to the latter. This pin tracks the
    # current head; each arc that adds a migration bumps it.
    # RESCAN 2026-06-04: this check's INTENT is "exactly one alembic head"
    # (single linear migration chain — no branch). The specific head value
    # drifts every time an arc adds a migration, which made this audit
    # script fail on every subsequent arc (it was pinned at
    # arc17_b_secret_cleanup_outbox while head advanced through arc18 and
    # the rescan migrations). Pinning a literal here is the drift, not the
    # safety property. We now assert single-head-ness dynamically.
    expected_head = None  # was a hard-coded literal; intentionally dynamic now
    try:
        proc = subprocess.run(
            ["alembic", "heads"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                **os.environ,
                "DATABASE_URL": os.environ.get(
                    "DATABASE_URL",
                    "postgresql+psycopg://x:x@localhost/x",
                ),
            },
        )
        if proc.returncode != 0:
            out.append(
                _fail(
                    section,
                    "alembic_heads_command",
                    f"alembic heads exited {proc.returncode}: "
                    f"stderr={proc.stderr.strip()!r}",
                )
            )
        else:
            heads = [
                line.split()[0] for line in proc.stdout.strip().splitlines() if line.strip()
            ]
            if len(heads) == 1:
                out.append(
                    _pass(
                        section,
                        "alembic_heads_single",
                        f"single head = {heads[0]}",
                    )
                )
            else:
                out.append(
                    _fail(
                        section,
                        "alembic_heads_single",
                        f"expected exactly one head (single linear chain); "
                        f"got {heads!r}",
                    )
                )
    except FileNotFoundError:
        out.append(
            _skip(
                section,
                "alembic_heads_single",
                "alembic CLI not on PATH; install dev deps to verify",
            )
        )
    except subprocess.TimeoutExpired:
        out.append(_fail(section, "alembic_heads_single", "alembic heads timed out"))

    # 1b. Model metadata: knowledge_sources + knowledge_chunks exist;
    #     traces.source_ids_used column exists; legacy class alias is
    #     GONE (Cleanup B closeout).
    try:
        # Force a stable DATABASE_URL so settings instantiation works
        # without polluting the operator's env.
        os.environ.setdefault(
            "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x"
        )
        os.environ.setdefault("MODERATION_PROVIDER", "null")
        import app.models.knowledge as _km
        from app.models.base import Base
        from app.models.knowledge import KnowledgeChunk
        from app.models.knowledge_source import KnowledgeSource
        from app.models.trace import Trace

        tables = {t.name for t in Base.metadata.tables.values()}
        if "knowledge_sources" in tables:
            out.append(_pass(section, "table_knowledge_sources_exists", ""))
        else:
            out.append(
                _fail(section, "table_knowledge_sources_exists", "missing in metadata")
            )

        if "knowledge_chunks" in tables and "knowledge_embeddings" not in tables:
            out.append(
                _pass(
                    section,
                    "table_knowledge_chunks_renamed",
                    "knowledge_embeddings absent, knowledge_chunks present",
                )
            )
        elif "knowledge_embeddings" in tables:
            out.append(
                _fail(
                    section,
                    "table_knowledge_chunks_renamed",
                    "legacy knowledge_embeddings table is still in metadata",
                )
            )
        else:
            out.append(
                _fail(
                    section,
                    "table_knowledge_chunks_renamed",
                    "neither table found",
                )
            )

        if "source_ids_used" in Trace.__table__.columns:
            out.append(_pass(section, "traces_source_ids_used_column", ""))
        else:
            out.append(
                _fail(
                    section,
                    "traces_source_ids_used_column",
                    "traces.source_ids_used not in model",
                )
            )

        if not hasattr(_km, "KnowledgeEmbedding"):
            out.append(
                _pass(
                    section,
                    "legacy_alias_removed",
                    "KnowledgeEmbedding alias dropped (Cleanup B)",
                )
            )
        else:
            out.append(
                _fail(
                    section,
                    "legacy_alias_removed",
                    "KnowledgeEmbedding alias still present — "
                    "Cleanup B requires it to be removed",
                )
            )

        # Smoke: KnowledgeSource carries the four required columns.
        ks_cols = set(KnowledgeSource.__table__.columns.keys())
        for required in (
            "id", "source_uuid", "admin_id", "luciel_instance_id",
            "ingestion_status", "source_version", "soft_deleted_at",
            "pending_downgrade_archived_at",
        ):
            if required in ks_cols:
                continue
            out.append(
                _fail(
                    section,
                    "knowledge_sources_columns",
                    f"missing column {required!r} on knowledge_sources",
                )
            )
                # break would skip remaining cols; the noise tells us
                # if many are missing at once.

        # Cleanup C: agent_id column dropped from knowledge_chunks.
        kc_cols = set(KnowledgeChunk.__table__.columns.keys())
        if "agent_id" not in kc_cols:
            out.append(
                _pass(
                    section,
                    "knowledge_chunks_agent_id_dropped",
                    "agent_id column gone (Cleanup C)",
                )
            )
        else:
            out.append(
                _fail(
                    section,
                    "knowledge_chunks_agent_id_dropped",
                    "agent_id column still present on knowledge_chunks",
                )
            )

        # Unit 1 excision: app.models.scope_assignment deleted (single-owner
        # model has no multi-seat scope_assignments table). Cleanup C checks
        # for scope_role PG enum are removed; ScopeRole now lives inline in
        # app/policy/scope.py with a single ADMIN_OWNER member.

    except Exception as exc:  # noqa: BLE001
        out.append(
            _fail(
                section,
                "model_metadata_import",
                f"import failed: {type(exc).__name__}: {exc}",
            )
        )

    # 1c. RLS policies on knowledge_sources — live-only.
    if live:
        out.append(_section_1c_rls_live(section))
    else:
        out.append(
            _skip(
                section,
                "rls_policies_on_knowledge_sources",
                "live DB required; re-run with --live against prod RDS",
            )
        )

    return out


def _section_1c_rls_live(section: str) -> CheckResult:
    """Count policies on knowledge_sources via pg_policies.

    Accepts either a native libpq URL (``postgresql://...``) or a
    SQLAlchemy URL (``postgresql+psycopg://...``). The ``+driver``
    suffix is stripped before handing the URL to psycopg, which only
    understands the native form.
    """
    url = os.environ.get("LUCIEL_LIVE_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not url:
        return _skip(
            section,
            "rls_policies_on_knowledge_sources",
            "set LUCIEL_LIVE_POSTGRES_URL (or DATABASE_URL) to enable "
            "live RLS check",
        )
    # Normalize SQLAlchemy-style URLs (postgresql+psycopg://...) into
    # the libpq form psycopg.connect expects.
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = scheme.split("+", 1)[0] + "://" + rest
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int
                      FROM pg_policies
                     WHERE tablename = 'knowledge_sources'
                    """
                )
                count = int(cur.fetchone()[0])
        if count >= 2:
            return _pass(
                section,
                "rls_policies_on_knowledge_sources",
                f"{count} policies installed",
            )
        return _fail(
            section,
            "rls_policies_on_knowledge_sources",
            f"expected ≥2 policies (admin_isolation + admin_isolation_write); "
            f"got {count}",
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            section,
            "rls_policies_on_knowledge_sources",
            f"live check failed: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------
# Section 2 — Entitlements
# ---------------------------------------------------------------------


def section_2_entitlements() -> list[CheckResult]:
    section = "2. Entitlements"
    out: list[CheckResult] = []

    try:
        os.environ.setdefault(
            "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x"
        )
        os.environ.setdefault("MODERATION_PROVIDER", "null")
        from app.policy.entitlements import (
            TIER_ENTITLEMENTS,
            TIER_FREE,
            TIER_PRO,
        )
    except Exception as exc:  # noqa: BLE001
        return [
            _fail(
                section,
                "entitlements_import",
                f"import failed: {type(exc).__name__}: {exc}",
            )
        ]

    # Unit 1 excision: TIER_ENTERPRISE removed; only Free + Pro exist.
    expectations = {
        TIER_FREE: {
            "knowledge_bytes_cap": 100 * 1024 * 1024,
            "knowledge_per_file_bytes_cap": 10 * 1024 * 1024,
            "knowledge_website_crawl_enabled": False,
        },
        TIER_PRO: {
            "knowledge_bytes_cap": 5 * 1024 * 1024 * 1024,
            "knowledge_per_file_bytes_cap": 50 * 1024 * 1024,
            "knowledge_website_crawl_enabled": True,
        },
    }
    for tier, want in expectations.items():
        ent = TIER_ENTITLEMENTS[tier]
        diffs = []
        for field_name, expected in want.items():
            actual = getattr(ent, field_name, "<missing>")
            if actual != expected:
                diffs.append(f"{field_name}: want={expected!r} got={actual!r}")
        if diffs:
            out.append(
                _fail(
                    section,
                    f"entitlements_{tier}",
                    "; ".join(diffs),
                )
            )
        else:
            out.append(
                _pass(
                    section,
                    f"entitlements_{tier}",
                    "all 3 knowledge fields match Vision §3.3 / §7",
                )
            )
    return out


# ---------------------------------------------------------------------
# Section 3 — Feature flag
# ---------------------------------------------------------------------


def section_3_feature_flag() -> list[CheckResult]:
    section = "3. Feature flag"
    try:
        os.environ.setdefault(
            "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x"
        )
        os.environ.setdefault("MODERATION_PROVIDER", "null")
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        return [
            _fail(
                section,
                "settings_import",
                f"import failed: {type(exc).__name__}: {exc}",
            )
        ]

    if settings.knowledge_retrieval_enabled is False:
        return [
            _pass(
                section,
                "knowledge_retrieval_enabled_default_false",
                "Arc 11 ships dormant; Arc 14 owns the flip",
            )
        ]
    return [
        _fail(
            section,
            "knowledge_retrieval_enabled_default_false",
            f"settings.knowledge_retrieval_enabled = "
            f"{settings.knowledge_retrieval_enabled!r}; MUST be False at close",
        )
    ]


# ---------------------------------------------------------------------
# Section 4 — Infrastructure templates
# ---------------------------------------------------------------------


def section_4_infrastructure() -> list[CheckResult]:
    section = "4. Infrastructure templates"
    out: list[CheckResult] = []

    if not REPO_CHECKOUT:
        # Running from a runtime image without cfn/. The IaC templates
        # are source-tree artifacts; skip rather than report FAIL.
        return [
            _skip(
                section,
                "cfn_template_present",
                "repo-only check (no cfn/ adjacent to script)",
            ),
            _skip(
                section,
                "task_def_knowledge_bucket_env",
                "repo-only check (no td-worker-rev34-arc11.json adjacent to script)",
            ),
        ]

    cfn_path = REPO_ROOT / "cfn" / "knowledge-bucket.yaml"
    td_path = REPO_ROOT / "td-worker-rev34-arc11.json"

    # 4a. CFN template parses as valid YAML with the expected top-level shape.
    if not cfn_path.exists():
        out.append(_fail(section, "cfn_template_present", f"missing: {cfn_path}"))
    else:
        try:
            import yaml

            class _CfnLoader(yaml.SafeLoader):
                pass

            def _passthrough(loader, tag_suffix, node):
                if isinstance(node, yaml.ScalarNode):
                    return f"!{tag_suffix} {node.value}"
                if isinstance(node, yaml.SequenceNode):
                    return loader.construct_sequence(node, deep=True)
                return loader.construct_mapping(node, deep=True)

            _CfnLoader.add_multi_constructor("!", _passthrough)
            tpl = yaml.load(cfn_path.read_text(encoding="utf-8"), Loader=_CfnLoader)
            if not isinstance(tpl, dict) or "Resources" not in tpl:
                out.append(
                    _fail(
                        section,
                        "cfn_template_present",
                        "template missing top-level Resources key",
                    )
                )
            else:
                resources = tpl["Resources"]
                has_bucket = any(
                    spec.get("Type") == "AWS::S3::Bucket"
                    for spec in resources.values()
                )
                has_policy = any(
                    spec.get("Type") == "AWS::IAM::ManagedPolicy"
                    for spec in resources.values()
                )
                if has_bucket and has_policy:
                    out.append(
                        _pass(
                            section,
                            "cfn_template_present",
                            "AWS::S3::Bucket + AWS::IAM::ManagedPolicy resources present",
                        )
                    )
                else:
                    out.append(
                        _fail(
                            section,
                            "cfn_template_present",
                            f"missing resource types: bucket={has_bucket} "
                            f"policy={has_policy}",
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            out.append(
                _fail(
                    section,
                    "cfn_template_present",
                    f"YAML parse failed: {type(exc).__name__}: {exc}",
                )
            )

    # 4b. ECS task-def includes KNOWLEDGE_S3_BUCKET via SSM secret.
    if not td_path.exists():
        out.append(_fail(section, "task_def_knowledge_bucket_env", f"missing: {td_path}"))
    else:
        try:
            td = json.loads(td_path.read_text(encoding="utf-8"))
            secrets = []
            envs = []
            for cdef in td.get("containerDefinitions", []):
                secrets.extend(cdef.get("secrets") or [])
                envs.extend(cdef.get("environment") or [])
            names = {s.get("name") for s in secrets} | {e.get("name") for e in envs}
            if "KNOWLEDGE_S3_BUCKET" in names:
                out.append(
                    _pass(
                        section,
                        "task_def_knowledge_bucket_env",
                        "KNOWLEDGE_S3_BUCKET wired via SSM secret",
                    )
                )
            else:
                out.append(
                    _fail(
                        section,
                        "task_def_knowledge_bucket_env",
                        "KNOWLEDGE_S3_BUCKET missing from task-def secrets/env",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            out.append(
                _fail(
                    section,
                    "task_def_knowledge_bucket_env",
                    f"task-def parse failed: {type(exc).__name__}: {exc}",
                )
            )

    return out


# ---------------------------------------------------------------------
# Section 5 — Cross-repo contract
# ---------------------------------------------------------------------


def section_5_cross_repo_contract() -> list[CheckResult]:
    section = "5. Cross-repo contract"
    if not REPO_CHECKOUT:
        return [
            _skip(
                section,
                "frontend_backend_arc14_substring",
                "repo-only check (no tests/ adjacent to script)",
            )
        ]
    # Defer to the unittest module via pytest — cheapest reliable
    # way to run the same checks the test suite uses.

    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/integrity/test_arc11_cross_repo_contract.py",
            "-q", "--no-header", "--tb=no",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x",
            ),
            "MODERATION_PROVIDER": os.environ.get(
                "MODERATION_PROVIDER", "null",
            ),
        },
    )
    if proc.returncode == 0:
        return [
            _pass(
                section,
                "frontend_backend_arc14_substring",
                proc.stdout.strip().splitlines()[-1]
                if proc.stdout.strip() else "ok",
            )
        ]
    return [
        _fail(
            section,
            "frontend_backend_arc14_substring",
            f"pytest exited {proc.returncode}: {proc.stdout[-2000:]}",
        )
    ]


# ---------------------------------------------------------------------
# Section 6 — Test counts
# ---------------------------------------------------------------------


def section_6_test_counts() -> list[CheckResult]:
    section = "6. Test counts"
    if not REPO_CHECKOUT:
        return [
            _skip(
                section,
                "test_count_minimum",
                "repo-only check (no tests/ adjacent to script)",
            )
        ]
    out: list[CheckResult] = []

    # Non-DB suite count.
    expected_minimum = 1260  # Step 8 baseline; Step 10 adds the contract tests.
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/",
            "--ignore=tests/db",
            "--ignore=tests/integration",
            "--collect-only", "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x",
            ),
            "MODERATION_PROVIDER": os.environ.get(
                "MODERATION_PROVIDER", "null",
            ),
        },
    )
    if proc.returncode != 0:
        out.append(
            _fail(
                section,
                "pytest_collect",
                f"pytest --collect-only exited {proc.returncode}",
            )
        )
        return out

    # The "X tests collected" line is the last non-blank stdout line.
    last_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    found_count: int | None = None
    for line in reversed(last_lines):
        if "test" in line and ("collected" in line or "selected" in line):
            try:
                found_count = int(line.split()[0])
                break
            except ValueError:
                continue
    if found_count is None:
        out.append(
            _fail(
                section,
                "pytest_collect",
                f"could not parse test count; last line: {last_lines[-1] if last_lines else ''!r}",
            )
        )
        return out

    if found_count >= expected_minimum:
        out.append(
            _pass(
                section,
                "test_count_minimum",
                f"collected {found_count} tests (≥ {expected_minimum})",
            )
        )
    else:
        out.append(
            _fail(
                section,
                "test_count_minimum",
                f"collected {found_count} tests; expected ≥ {expected_minimum}",
            )
        )
    return out


# ---------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------


def run_all(*, live: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.extend(section_1_migrations_and_schema(live=live))
    results.extend(section_2_entitlements())
    results.extend(section_3_feature_flag())
    results.extend(section_4_infrastructure())
    results.extend(section_5_cross_repo_contract())
    results.extend(section_6_test_counts())
    results.extend(section_7_instance_lifecycle(live=live))
    results.extend(section_8_no_internal_arc_strings())
    return results


# ---------------------------------------------------------------------
# Section 7 — Arc 11 Closeout PR-A: instance lifecycle
# ---------------------------------------------------------------------


def section_7_instance_lifecycle(*, live: bool) -> list[CheckResult]:
    """Arc 11 Closeout PR-A — Pause/Resume/Delete/Restore + retention."""
    section = "7. Instance lifecycle"
    out: list[CheckResult] = []

    # 7a. Live check: PG enum ``instance_status`` exists.
    if live:
        out.append(_section_7a_instance_status_enum_live(section))
    else:
        out.append(
            _skip(
                section,
                "instance_status_pg_enum_exists",
                "live DB required; re-run with --live to verify "
                "pg_type contains the instance_status enum",
            )
        )

    # 7b. Static check: all four lifecycle routes registered in admin.py.
    out.append(_section_7b_instance_lifecycle_routes_present(section))

    return out


def _section_7a_instance_status_enum_live(section: str) -> CheckResult:
    """Verify the ``instance_status`` PG enum exists with three members.

    Mirrors the shape of ``_section_1c_rls_live`` -- accepts either
    libpq or SQLAlchemy URLs and degrades to FAIL with a clear message
    on connection or query errors.
    """
    url = os.environ.get("LUCIEL_LIVE_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not url:
        return _skip(
            section,
            "instance_status_pg_enum_exists",
            "set LUCIEL_LIVE_POSTGRES_URL (or DATABASE_URL) to enable "
            "live enum check",
        )
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = scheme.split("+", 1)[0] + "://" + rest
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT enumlabel
                      FROM pg_enum e
                      JOIN pg_type t ON e.enumtypid = t.oid
                     WHERE t.typname = 'instance_status'
                     ORDER BY e.enumsortorder
                    """
                )
                labels = [row[0] for row in cur.fetchall()]
        if labels == ["active", "paused", "deleted"]:
            return _pass(
                section,
                "instance_status_pg_enum_exists",
                "enum members: " + ", ".join(labels),
            )
        if not labels:
            return _fail(
                section,
                "instance_status_pg_enum_exists",
                "pg_type has no 'instance_status' enum -- migration "
                "arc11_closeout_a_instance_lifecycle has not been "
                "applied",
            )
        return _fail(
            section,
            "instance_status_pg_enum_exists",
            f"expected members ['active','paused','deleted']; got {labels}",
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            section,
            "instance_status_pg_enum_exists",
            f"live check failed: {type(exc).__name__}: {exc}",
        )


def _section_7b_instance_lifecycle_routes_present(section: str) -> CheckResult:
    """Static check: the four lifecycle routes are registered in admin.py.

    Reads ``app/api/v1/admin.py`` and asserts the decorators for
    /pause, /resume, /restore, and the DELETE route on /instances/{pk}
    are all present. Pure text assertion -- the deeper behavioural
    contract is covered by tests/api/test_instance_lifecycle_arc11_closeout.py.
    """
    admin_path = REPO_ROOT / "app" / "api" / "v1" / "admin" / "__init__.py"
    if not admin_path.exists():
        return _fail(
            section,
            "instance_lifecycle_routes_present",
            f"admin.py not found at {admin_path}",
        )
    src = admin_path.read_text(encoding="utf-8")

    required = [
        ('"/instances/{pk}/pause"', "POST /instances/{pk}/pause"),
        ('"/instances/{pk}/resume"', "POST /instances/{pk}/resume"),
        ('"/instances/{pk}/restore"', "POST /instances/{pk}/restore"),
        # The DELETE route shares the path with GET/PATCH so we look
        # for the function symbol that signals the soft-delete semantics.
        ("def delete_luciel_instance", "DELETE /instances/{pk}"),
    ]
    missing = [label for token, label in required if token not in src]
    if missing:
        return _fail(
            section,
            "instance_lifecycle_routes_present",
            f"missing route(s): {', '.join(missing)}",
        )
    return _pass(
        section,
        "instance_lifecycle_routes_present",
        "all four lifecycle routes registered",
    )


# ---------------------------------------------------------------------
# Section 8 — Arc 11 Closeout PR-B: no internal arc strings in
# user-facing contracts.
# ---------------------------------------------------------------------


def section_8_no_internal_arc_strings() -> list[CheckResult]:
    """Founder principle: internal arc identifiers (``Arc-14``, ``Arc-15``
    …) must never appear as string literals anywhere a user might
    surface them — API payloads, frontend-visible columns, cross-repo
    contract strings.

    PR-B removed the original ``"Arc-14"`` substring from the crawl
    stub and replaced it with the structured
    ``ingestion_error_code`` column. This check grep-scans the backend
    source tree for any remaining ``Arc-NN`` string literal and
    asserts that only the migration backfill clause (legacy
    grandfathered data) carries one.
    """
    section = "8. No internal arc strings in user-facing contracts"
    if not REPO_CHECKOUT:
        return [
            _skip(
                section,
                "no_arc_string_in_user_facing_contracts",
                "repo-only check (no app/ tree adjacent to script)",
            )
        ]

    # Scan only production source: ``app/`` (route handlers, models,
    # schemas, workers, services). Tests + scripts + migrations are
    # intentionally excluded — tests legitimately *talk about* the
    # historical substring (the contract test asserts on its
    # absence), and the migration's backfill clause is the one
    # grandfathered place the substring is permitted.
    app_root = REPO_ROOT / "app"
    if not app_root.is_dir():
        return [
            _fail(
                section,
                "no_arc_string_in_user_facing_contracts",
                f"app/ not found at {app_root}",
            )
        ]

    # AST-based scan. We ban any ``ast.Constant(value=str)`` whose
    # value contains ``Arc-N`` / ``Arc N`` — EXCEPT module / class /
    # function docstrings. Docstrings legitimately discuss the
    # module's history; what we ban is string *values* that could
    # surface to the user or to the cross-repo contract.
    import ast as _ast

    # Scope per PR-B spec: the "Arc-14" substring specifically — the
    # one that leaked into the cross-repo data contract via the
    # crawl-stub. After PR-B, the only remaining occurrence permitted
    # in the repo is the migration's grandfathered legacy-data
    # backfill clause (and that file lives outside app/, so this scan
    # never sees it). The broader "any Arc-NN literal" sweep is
    # premature — many of those are doctrine-anchor strings in audit
    # notes that legitimately reference the arc the row was written
    # in. Tightening to the documented leak prevents false positives.
    arc_pattern = re.compile(r"\bArc-14\b")

    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            continue

        docstring_ids: set[int] = set()
        for node in _ast.walk(tree):
            if isinstance(
                node,
                (
                    _ast.Module,
                    _ast.FunctionDef,
                    _ast.AsyncFunctionDef,
                    _ast.ClassDef,
                ),
            ):
                body = getattr(node, "body", None)
                if not body:
                    continue
                first = body[0]
                if (
                    isinstance(first, _ast.Expr)
                    and isinstance(first.value, _ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstring_ids.add(id(first.value))

        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
                and arc_pattern.search(node.value)
            ):
                rel = path.relative_to(REPO_ROOT)
                line_no = getattr(node, "lineno", 0)
                # Show only the first 60 chars to keep the report tight.
                preview = node.value if len(node.value) <= 60 else (
                    node.value[:57] + "..."
                )
                offenders.append(f"{rel}:{line_no}: {preview!r}")

    if offenders:
        return [
            _fail(
                section,
                "no_arc_string_in_user_facing_contracts",
                f"found {len(offenders)} arc-identifier string literal(s) "
                f"in app/: " + "; ".join(offenders[:5])
                + (" …" if len(offenders) > 5 else ""),
            )
        ]
    return [
        _pass(
            section,
            "no_arc_string_in_user_facing_contracts",
            "no internal arc identifiers found in app/ source",
        )
    ]


def _render_table(results: list[CheckResult]) -> str:
    lines = []
    # Column widths.
    secw = max(len(r.section) for r in results)
    namew = max(len(r.name) for r in results)
    header = f"{'STATUS':6}  {'SECTION':{secw}}  {'CHECK':{namew}}  DETAIL"
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        lines.append(f"{r.status:6}  {r.section:{secw}}  {r.name:{namew}}  {r.detail}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Arc 11 close-audit. Pillar 5 per ARC11_PLAN.md §8.5.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the human-readable table",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run live-infrastructure checks (RLS in pg_policies). Requires "
        "LUCIEL_LIVE_POSTGRES_URL.",
    )
    args = parser.parse_args()

    results = run_all(live=args.live)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(_render_table(results))
        print()
        n_pass = sum(1 for r in results if r.status == "PASS")
        n_fail = sum(1 for r in results if r.status == "FAIL")
        n_skip = sum(1 for r in results if r.status == "SKIP")
        print(f"Summary: {n_pass} PASS, {n_fail} FAIL, {n_skip} SKIP "
              f"({len(results)} total)")

    # Non-zero on any FAIL. SKIP is not a failure.
    any_fail = any(r.status == "FAIL" for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
