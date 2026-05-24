"""Arc 7 C6 schema verification probe.

Verifies admins.last_signup_ip column shape + partial index + alembic head.
"""
import os, sys
from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
# alembic uses postgresql:// — sqlalchemy fine
eng = create_engine(url, future=True)
ok = True
with eng.connect() as conn:
    # 1. Column type + nullability
    r = conn.execute(text("""
        SELECT data_type, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='admins' AND column_name='last_signup_ip'
    """)).first()
    if r is None:
        print("FAIL: admins.last_signup_ip column missing"); ok = False
    else:
        data_type, udt_name, is_nullable = r
        print(f"column.data_type={data_type} udt_name={udt_name} is_nullable={is_nullable}")
        if udt_name != 'inet':
            print(f"FAIL: expected udt_name=inet, got {udt_name}"); ok = False
        if is_nullable != 'YES':
            print(f"FAIL: expected nullable, got {is_nullable}"); ok = False

    # 2. Partial index
    r = conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname='public' AND tablename='admins' AND indexname='ix_admins_last_signup_ip'
    """)).first()
    if r is None:
        print("FAIL: ix_admins_last_signup_ip missing"); ok = False
    else:
        print(f"index.def={r[0]}")
        if 'last_signup_ip IS NOT NULL' not in r[0] or 'active' not in r[0]:
            print("FAIL: partial index predicate missing IS NOT NULL/active"); ok = False

    # 3. Alembic head
    r = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    print(f"alembic.head={r[0]}")
    if r[0] != 'arc7_b_admins_last_signup_ip':
        print(f"FAIL: expected alembic head arc7_b_admins_last_signup_ip, got {r[0]}"); ok = False

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
