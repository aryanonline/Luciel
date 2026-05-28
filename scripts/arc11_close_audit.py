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
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]


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

    # 1a. alembic heads is single and points at the latest Arc 11
    # migration (Cleanup A's data_category rename is the post-Step-4
    # head once the no-deferrals closeout lands).
    expected_head = "arc11_cleanup_b_drop_legacy_source_columns"
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
            if heads == [expected_head]:
                out.append(
                    _pass(section, "alembic_heads_single", f"head = {expected_head}")
                )
            else:
                out.append(
                    _fail(
                        section,
                        "alembic_heads_single",
                        f"expected exactly one head {expected_head!r}; got {heads!r}",
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
    """Count policies on knowledge_sources via pg_policies."""
    url = os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
    if not url:
        return _skip(
            section,
            "rls_policies_on_knowledge_sources",
            "set LUCIEL_LIVE_POSTGRES_URL to enable live RLS check",
        )
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
            TIER_ENTERPRISE,
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
        TIER_ENTERPRISE: {
            "knowledge_bytes_cap": None,
            "knowledge_per_file_bytes_cap": 500 * 1024 * 1024,
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
    return results


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
