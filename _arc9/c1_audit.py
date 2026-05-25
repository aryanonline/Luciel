"""
Arc 9 C1 — tenant isolation column audit.

Scans every SQLAlchemy model in app/models/ and reports for each:
  - admin_id     (Wall 1 — Account-level isolation)
  - instance_id / luciel_instance_id (Wall 3 — Instance-level isolation)
  - session_id   (Wall 4 — Lead/session-level isolation)

For each column we capture: present? non-null? FK target? indexed?

Output: _arc9/C1_audit_findings.md (human review) + _arc9/C1_audit_raw.json
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[1] / "app" / "models"
OUT_DIR = Path(__file__).resolve().parent
# Wall 1 has TWO accepted column names per Arc 5 Revision C:
#   - 'admin_id'  (new canonical)
#   - 'tenant_id' (legacy alias kept for call-site compat; FK to admins.id)
# Both reach the Account boundary; both count for Wall 1 enforcement.
WALL_COLS = {
    "wall1_admin_id": ("admin_id", "tenant_id"),
    "wall3_instance_id": ("instance_id", "luciel_instance_id"),
    "wall4_session_id": ("session_id",),
}

# Tables that are platform-internal infra (NOT customer data) — flagged but expected to lack walls.
PLATFORM_TABLES = {
    "admins",            # the Account itself — IS the admin_id, doesn't carry one
    "users",             # durable identity, can hold scope across admins (Q5)
    "email_suppression", # SES global blocklist — platform-level
    "email_send_event",  # SES feedback telemetry — platform-level operational data
    "deletion_logs",     # post-deletion forensic record — keep but it has tenant_id anyway
}


@dataclass
class ColumnFact:
    present: bool = False
    nullable: bool | None = None
    fk_target: str | None = None
    indexed_explicit: bool = False
    raw_line: str = ""


@dataclass
class ModelFact:
    file: str
    class_name: str
    tablename: str | None
    cols: dict[str, ColumnFact] = field(default_factory=dict)


COLUMN_RE = re.compile(
    r"(?P<name>\w+)\s*=\s*(?:mapped_column|Column)\s*\((?P<args>.*?)\)\s*$",
    re.DOTALL,
)


def parse_column_call(name: str, args_src: str) -> ColumnFact:
    """Best-effort static parse of a Column() / mapped_column() call.

    args_src is the full source segment of the assignment, possibly spanning
    many lines. We scan the entire string with DOTALL-friendly patterns.
    """
    cf = ColumnFact(present=True, raw_line=args_src.strip()[:300])
    # nullable= (anywhere in segment)
    m = re.search(r"nullable\s*=\s*(True|False)", args_src)
    if m:
        cf.nullable = m.group(1) == "True"
    # ForeignKey("...") — also handle ForeignKey( newline "..." )
    m = re.search(r"""ForeignKey\(\s*['"]([^'"]+)['"]""", args_src, re.DOTALL)
    if m:
        cf.fk_target = m.group(1)
    # index=True (anywhere)
    if re.search(r"index\s*=\s*True", args_src):
        cf.indexed_explicit = True
    return cf


def scan_model_file(path: Path) -> list[ModelFact]:
    src = path.read_text()
    tree = ast.parse(src)
    facts: list[ModelFact] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # only consider classes that inherit from Base or that have __tablename__
        tablename = None
        col_assigns = {}
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                tgt = stmt.targets[0].id
                if tgt == "__tablename__" and isinstance(stmt.value, ast.Constant):
                    tablename = stmt.value.value
                else:
                    # capture raw source slice via segment for any column-shaped assign
                    try:
                        seg = ast.get_source_segment(src, stmt) or ""
                    except Exception:
                        seg = ""
                    if "Column(" in seg or "mapped_column(" in seg:
                        col_assigns[tgt] = seg
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                tgt = stmt.target.id
                try:
                    seg = ast.get_source_segment(src, stmt) or ""
                except Exception:
                    seg = ""
                if "Column(" in seg or "mapped_column(" in seg:
                    col_assigns[tgt] = seg

        if tablename is None and not col_assigns:
            continue

        mf = ModelFact(file=str(path.relative_to(MODELS_DIR.parent.parent)),
                       class_name=node.name, tablename=tablename)
        for wall_key, candidates in WALL_COLS.items():
            for cand in candidates:
                if cand in col_assigns:
                    cf = parse_column_call(cand, col_assigns[cand])
                    # store under canonical wall key with actual column name
                    mf.cols[f"{wall_key}::{cand}"] = cf
                    break
        # also record presence/absence even when col not present
        for wall_key, candidates in WALL_COLS.items():
            if not any(k.startswith(wall_key) for k in mf.cols):
                mf.cols[wall_key] = ColumnFact(present=False)
        facts.append(mf)
    return facts


def post_process(all_facts: list[ModelFact]) -> None:
    """Apply doctrine rules after the static scan:

    - tenant_id always counts as Wall 1 per Arc 5 Revision C (legacy alias for
      admin_id). Missing FK to admins.id is a C8 schema-delta finding, not a
      Wall 1 absence.
    - luciel_instances.id no longer exists (renamed to instances.id at Arc 5).
      Tables still pointing there carry a latent foreign-key drift that C8
      must repair.
    """
    # no-op for now — wall detection happens in scan_model_file; this hook
    # stays for future doctrine rules (e.g. excluding unrelated 'tenant_id'
    # columns if any are found that aren't account-bound).


def render_markdown(all_facts: list[ModelFact]) -> str:
    rows = []
    for mf in sorted(all_facts, key=lambda x: (x.tablename or "zzz", x.class_name)):
        tn = mf.tablename or "(no __tablename__)"
        is_platform = tn in PLATFORM_TABLES

        def cell(wall: str) -> str:
            # find any col matching this wall
            matches = [(k, v) for k, v in mf.cols.items() if k.startswith(wall) and v.present]
            if not matches:
                return "❌ absent"
            k, cf = matches[0]
            col_name = k.split("::", 1)[1]
            bits = [f"`{col_name}`"]
            if cf.nullable is True:
                bits.append("nullable")
            elif cf.nullable is False:
                bits.append("NOT NULL")
            if cf.fk_target:
                bits.append(f"FK→`{cf.fk_target}`")
            if cf.indexed_explicit:
                bits.append("idx")
            return "✅ " + " · ".join(bits)

        row = (
            f"| `{tn}` | `{mf.class_name}` | "
            f"{cell('wall1_admin_id')} | {cell('wall3_instance_id')} | {cell('wall4_session_id')} | "
            f"{'platform' if is_platform else 'customer-data'} |"
        )
        rows.append(row)

    md = [
        "# Arc 9 C1 — Tenant-isolation column audit",
        "",
        "**Status:** RECON (no code change). Auto-generated by `_arc9/c1_audit.py`.",
        "**Generated against:** branch `arc-9-c1-recon`, repo HEAD at C1 open.",
        "**Source of truth:** `app/models/*.py`. Where this disagrees with the live DB schema, the live DB wins — re-run with a connected DB introspection in C1.5 if needed.",
        "",
        "## Reading the table",
        "",
        "- ✅ = column present in model; `NOT NULL` / `nullable` / `FK→…` / `idx` flags from the model definition.",
        "- ❌ absent = no such column declared in the model.",
        "- **classification**: `customer-data` tables MUST carry every applicable wall column; `platform` tables are infra and exempt by design (called out individually in §3).",
        "",
        "## §1 — Per-table column matrix",
        "",
        "| Table | Model class | Wall 1 (`admin_id`) | Wall 3 (`instance_id`) | Wall 4 (`session_id`) | Classification |",
        "|---|---|---|---|---|---|",
    ] + rows + [
        "",
        "## §2 — Findings (gaps to fix in C3 / C4 / C5)",
        "",
        "Populated below from the matrix above. See `C1_audit_raw.json` for machine-readable form.",
        "",
    ]
    return "\n".join(md)


def main() -> None:
    all_facts: list[ModelFact] = []
    for py in sorted(MODELS_DIR.glob("*.py")):
        if py.name in {"__init__.py", "base.py"}:
            continue
        all_facts.extend(scan_model_file(py))
    post_process(all_facts)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "C1_audit_raw.json").write_text(
        json.dumps([asdict(f) for f in all_facts], indent=2, default=str)
    )
    # Write the auto-generated matrix to its own file; the human-authored
    # findings live in C1_audit_findings.md and \include\ this matrix.
    # This keeps re-runs idempotent — the script never clobbers human prose.
    (OUT_DIR / "C1_audit_matrix.md").write_text(render_markdown(all_facts))
    print(f"Scanned {len(all_facts)} model classes across {len(list(MODELS_DIR.glob('*.py')))} files.")
    print(f"Wrote: {OUT_DIR / 'C1_audit_matrix.md'}  (auto-regenerated each run)")
    print(f"Wrote: {OUT_DIR / 'C1_audit_raw.json'}")
    print("Human-authored findings live in: C1_audit_findings.md (do not regenerate).")


if __name__ == "__main__":
    main()
