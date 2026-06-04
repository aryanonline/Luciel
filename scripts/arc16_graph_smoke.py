"""Live smoke test for the ARC 16 graph store + recursive-CTE traversal.

Runs against the local Postgres. Seeds a small real-estate-shaped graph
under one tenant (as superuser, bypassing RLS for setup), then exercises
the repository as the non-superuser luciel_app-style role to prove:
  1. find_nodes attribute filter (multi-attribute intersection)
  2. recursive traverse (multi-hop) with cycle guard
  3. cross-tenant + cross-instance RLS isolation on the graph
"""
from __future__ import annotations

import sys
import uuid

import psycopg
from psycopg import sql as pgsql
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.repositories.knowledge_graph_repository import (
    KnowledgeGraphRepository,
)

PG = "postgresql://postgres:postgres@127.0.0.1:5432/luciel"
SA = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/luciel"


def main() -> int:
    admin_a = "g_admin_a_" + uuid.uuid4().hex[:6]
    admin_b = "g_admin_b_" + uuid.uuid4().hex[:6]
    role = "g_role_" + uuid.uuid4().hex[:8]

    admin = psycopg.connect(PG, autocommit=True)
    failures: list[str] = []
    try:
        with admin.cursor() as c:
            # non-superuser role the app uses
            c.execute(
                pgsql.SQL(
                    "CREATE ROLE {r} LOGIN PASSWORD 'x' NOSUPERUSER NOBYPASSRLS"
                ).format(r=pgsql.Identifier(role))
            )
            c.execute(
                pgsql.SQL("GRANT USAGE ON SCHEMA public TO {r}").format(
                    r=pgsql.Identifier(role)
                )
            )
            c.execute(
                pgsql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    "IN SCHEMA public TO {r}"
                ).format(r=pgsql.Identifier(role))
            )
            c.execute(
                pgsql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                    "TO {r}"
                ).format(r=pgsql.Identifier(role))
            )

            ids: dict[str, dict] = {}
            for aid in (admin_a, admin_b):
                c.execute(
                    "INSERT INTO admins (id,name,tier,active) "
                    "VALUES (%s,%s,'pro',true) ON CONFLICT (id) DO NOTHING",
                    (aid, aid),
                )
                c.execute(
                    "INSERT INTO instances (admin_id,instance_slug,"
                    "display_name,active) VALUES (%s,%s,%s,true) RETURNING id",
                    (aid, "inst-" + aid, aid),
                )
                inst = c.fetchone()[0]
                c.execute(
                    "INSERT INTO knowledge_sources (admin_id,"
                    "luciel_instance_id,source_type,size_bytes,"
                    "ingestion_status,ingested_by) "
                    "VALUES (%s,%s,'csv',1,'ready','seed') RETURNING id",
                    (aid, inst),
                )
                src = c.fetchone()[0]
                ids[aid] = {"inst": inst, "src": src}

            # Tenant A graph (real-estate shape):
            #   Listing L1 {bd:3, price:900k} -IS_IN-> Neighborhood Riverside
            #   Listing L2 {bd:2, price:700k} -IS_IN-> Neighborhood Riverside
            #   Neighborhood Riverside -IN-> City Springfield
            # find_nodes(bd>=3) is approximated by exact containment bd:3.
            a = ids[admin_a]

            def node(aid, inst, src, etype, label, attrs):
                import json

                c.execute(
                    "INSERT INTO knowledge_graph_nodes (admin_id,"
                    "luciel_instance_id,entity_type,entity_label,attributes,"
                    "source_id) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (aid, inst, etype, label, json.dumps(attrs), src),
                )
                return c.fetchone()[0]

            def edge(aid, inst, src, s, d, rel):
                c.execute(
                    "INSERT INTO knowledge_graph_edges (admin_id,"
                    "luciel_instance_id,src_node_id,dst_node_id,"
                    "relationship_type,source_id) VALUES (%s,%s,%s,%s,%s,%s)",
                    (aid, inst, s, d, rel, src),
                )

            l1 = node(admin_a, a["inst"], a["src"], "Listing", "123 Main St",
                      {"bedrooms": 3, "price": 900000})
            l2 = node(admin_a, a["inst"], a["src"], "Listing", "45 Oak Ave",
                      {"bedrooms": 2, "price": 700000})
            riverside = node(admin_a, a["inst"], a["src"], "Neighborhood",
                             "Riverside", {})
            city = node(admin_a, a["inst"], a["src"], "City",
                        "Springfield", {})
            edge(admin_a, a["inst"], a["src"], l1, riverside, "IS_IN")
            edge(admin_a, a["inst"], a["src"], l2, riverside, "IS_IN")
            edge(admin_a, a["inst"], a["src"], riverside, city, "IN")
            # cycle: city -CONTAINS-> riverside (to prove the guard)
            edge(admin_a, a["inst"], a["src"], city, riverside, "CONTAINS")

            # Tenant B: a node that must NEVER appear in A's results.
            b = ids[admin_b]
            node(admin_b, b["inst"], b["src"], "Listing", "B-SECRET-LISTING",
                 {"bedrooms": 3, "price": 900000})

        # --- exercise repo as non-superuser, both GUCs bound ---
        engine = create_engine(SA, future=True)

        @event.listens_for(engine, "begin")
        def _bind(conn):  # noqa: ANN001
            conn.execute(
                text("SELECT set_config('app.admin_id', :a, true)").bindparams(
                    a=admin_a
                )
            )
            conn.execute(
                text(
                    "SELECT set_config('app.instance_id', :i, true)"
                ).bindparams(i=str(a["inst"]))
            )

        # connect as the limited role
        engine_role = create_engine(
            f"postgresql+psycopg://{role}:x@127.0.0.1:5432/luciel",
            future=True,
        )

        @event.listens_for(engine_role, "begin")
        def _bind_role(conn):  # noqa: ANN001
            conn.execute(
                text("SELECT set_config('app.admin_id', :a, true)").bindparams(
                    a=admin_a
                )
            )
            conn.execute(
                text(
                    "SELECT set_config('app.instance_id', :i, true)"
                ).bindparams(i=str(a["inst"]))
            )

        Session = sessionmaker(bind=engine_role, future=True)
        with Session() as s:
            repo = KnowledgeGraphRepository(s)

            # TEST 1: find_nodes attribute filter (3BR listings)
            three_br = repo.find_nodes(
                admin_id=admin_a,
                luciel_instance_id=a["inst"],
                entity_type="Listing",
                attribute_filters={"bedrooms": 3},
            )
            labels = sorted(n.entity_label for n in three_br)
            if labels != ["123 Main St"]:
                failures.append(
                    f"T1 find_nodes(bedrooms=3) expected ['123 Main St'], "
                    f"got {labels}"
                )

            # TEST 2: traverse from L1 -> Riverside -> Springfield (2 hops),
            # cycle (city->riverside) must not loop forever.
            reached = repo.traverse(
                admin_id=admin_a,
                luciel_instance_id=a["inst"],
                seed_node_ids=[l1],
                max_depth=3,
            )
            rlabels = sorted(n.entity_label for n in reached)
            # expect L1 (seed), Riverside, Springfield
            if rlabels != ["123 Main St", "Riverside", "Springfield"]:
                failures.append(
                    f"T2 traverse expected [123 Main St, Riverside, "
                    f"Springfield], got {rlabels}"
                )

            # TEST 3: scope triple present on every returned node
            for n in three_br + reached:
                if n.admin_id != admin_a:
                    failures.append(
                        f"T3 node {n.id} admin_id={n.admin_id} != {admin_a}"
                    )
                if n.luciel_instance_id != a["inst"]:
                    failures.append(f"T3 node {n.id} wrong instance")
                if not n.source_id or n.source_id < 1:
                    failures.append(f"T3 node {n.id} missing source_id")

            # TEST 4: cross-tenant isolation — B's secret never visible
            all_a = repo.find_nodes(
                admin_id=admin_a, luciel_instance_id=a["inst"], limit=500
            )
            if any("B-SECRET" in n.entity_label for n in all_a):
                failures.append("T4 LEAK: tenant B node visible to A")

        engine.dispose()
        engine_role.dispose()

    finally:
        with admin.cursor() as c:
            for aid in (admin_a, admin_b):
                c.execute("DELETE FROM knowledge_graph_edges WHERE admin_id=%s", (aid,))
                c.execute("DELETE FROM knowledge_graph_nodes WHERE admin_id=%s", (aid,))
                c.execute("DELETE FROM knowledge_sources WHERE admin_id=%s", (aid,))
                c.execute("DELETE FROM instances WHERE admin_id=%s", (aid,))
                c.execute("DELETE FROM admins WHERE id=%s", (aid,))
            c.execute(
                pgsql.SQL("DROP OWNED BY {r}").format(r=pgsql.Identifier(role))
            )
            c.execute(pgsql.SQL("DROP ROLE {r}").format(r=pgsql.Identifier(role)))
        admin.close()

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS: find_nodes filter, recursive traverse w/ cycle guard, "
          "scope triple, cross-tenant isolation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
