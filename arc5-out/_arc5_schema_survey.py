"""
Arc 5 schema survey — AST-based, not grep-based.

For each model file in app/models/, parse the AST and find:
- The __tablename__ string (the actual SQL table name)
- All `mapped_column(...)` or `Column(...)` declarations
- Of those, which ones are scope FKs (tenant_id, domain_id, agent_id, luciel_instance_id)

This is the ground-truth survey for Arc 5 Revision C's drop list.
"""
import ast
import os

MODEL_DIR = "app/models"
SCOPE_FK_NAMES = {"tenant_id", "domain_id", "agent_id", "luciel_instance_id"}

def find_mapped_columns(node, current_class=None):
    """Recursively find all `name: Mapped[...] = mapped_column(...)` or `name = Column(...)` annotations."""
    results = []
    for child in ast.walk(node):
        # SQLAlchemy 2.0 style: `name: Mapped[T] = mapped_column(...)`
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name = child.target.id
            if name in SCOPE_FK_NAMES:
                results.append(name)
        # SQLAlchemy 1.x style: `name = Column(...)`
        elif isinstance(child, ast.Assign):
            for t in child.targets:
                if isinstance(t, ast.Name) and t.id in SCOPE_FK_NAMES:
                    # Verify the value is a Column(...) or mapped_column(...) call
                    if isinstance(child.value, ast.Call):
                        fn = child.value.func
                        fn_name = ""
                        if isinstance(fn, ast.Name):
                            fn_name = fn.id
                        elif isinstance(fn, ast.Attribute):
                            fn_name = fn.attr
                        if fn_name in ("Column", "mapped_column"):
                            results.append(t.id)
    return results

def find_tablename(class_node):
    """Find __tablename__ = '...' in a class body."""
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "__tablename__":
                    if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        return stmt.value.value
    return None

results = {}

for fname in sorted(os.listdir(MODEL_DIR)):
    if not fname.endswith(".py"):
        continue
    if fname == "__init__.py":
        continue
    path = os.path.join(MODEL_DIR, fname)
    with open(path) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError as e:
            print(f"SyntaxError in {fname}: {e}")
            continue

    # Find every ORM class in the file (a class with __tablename__)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            tn = find_tablename(node)
            if tn:
                # Recurse INTO this class only, not the whole tree
                cols = find_mapped_columns(node)
                # Deduplicate (a column might be re-mentioned in __table_args__, etc.)
                cols = sorted(set(cols))
                if cols:  # Only record classes that have at least one scope FK
                    results[(fname, node.name, tn)] = cols

# Print results in a clean table
print(f"{'File':<30} {'Class':<30} {'Table':<35} Scope FK columns")
print("-" * 130)
for (fname, cls, tn), cols in sorted(results.items()):
    print(f"{fname:<30} {cls:<30} {tn:<35} {', '.join(cols)}")

print()
print(f"Total classes with at least one scope FK: {len(results)}")
print(f"Total scope-FK column drops needed: {sum(len(c) for c in results.values())}")

# Also surface tables that are themselves being dropped (need to know to NOT drop columns first)
DROP_TABLES = {"tenants", "domains", "luciel_instances", "agents"}
print()
print("Tables that will be DROPPED entirely in Revision C (no column-drop needed):")
for (fname, cls, tn), cols in sorted(results.items()):
    if tn in DROP_TABLES:
        print(f"  {tn} (in {fname} class {cls}) — {len(cols)} columns would be dropped WITH the table")

print()
print("Column-drops needed on SURVIVING tables (the actual Revision C drop list):")
total_actual_drops = 0
for (fname, cls, tn), cols in sorted(results.items()):
    if tn not in DROP_TABLES:
        print(f"  {tn}: {', '.join(cols)}")
        total_actual_drops += len(cols)
print()
print(f"TOTAL column drops on surviving tables: {total_actual_drops}")
