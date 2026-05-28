"""Arc 11 Step 5 — TraceRepository.list_recent_traces_using_source.

Static contract for the read path that backs the
Architecture §3.2.2 delete-confirm modal preview. The HTTP
endpoint that exposes it is built in Step 7.

Static-shape tests are the project convention when the query
uses Postgres-specific operators that SQLite cannot emulate.
``traces.source_ids_used @> ARRAY[$id]::bigint[]`` and the
backing GIN index are exactly that case. The live-DB execution
test lives in ``tests/db/test_arc11_trace_affected_questions.py``
(opt-in via ``LUCIEL_LIVE_POSTGRES_URL``).

Locked contracts:

  C1  Method exists with the exact signature the brief specifies:
      ``list_recent_traces_using_source(*, admin_id, luciel_instance_id,
      source_id, limit=5) -> list[Trace]``. Every parameter is
      keyword-only.
  C2  The compiled SQL contains the three predicates: ``admin_id =``,
      ``luciel_instance_id =``, and ``source_ids_used @> :…``.
  C3  ``ORDER BY created_at DESC LIMIT``.
  C4  The array literal is typed ``::bigint[]`` so the GIN index
      from Arc 11 Step 1 is picked.
  C5  ``limit`` default is 5 (matches the §3.7 endpoint contract).
"""
from __future__ import annotations

import inspect
import unittest

from sqlalchemy.dialects import postgresql

from app.repositories.trace_repository import TraceRepository


class _NullDb:
    """Stand-in for ``Session`` — the test only inspects the
    compiled SQL, never executes it."""

    def scalars(self, _stmt):  # pragma: no cover - never called
        raise AssertionError("unexpected DB call")


def _compiled(statement) -> str:
    """Compile a SQLAlchemy statement against the Postgres dialect
    with literal parameter binding, so the test can grep the SQL
    text reliably."""
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestListRecentTracesUsingSourceContract(unittest.TestCase):
    """Locks the signature + the compiled SQL shape."""

    def test_c1_method_exists_with_kwonly_signature(self):
        method = getattr(
            TraceRepository, "list_recent_traces_using_source", None,
        )
        self.assertIsNotNone(
            method,
            "TraceRepository must expose list_recent_traces_using_source — "
            "the read path for the §3.2.2 modal preview.",
        )
        sig = inspect.signature(method)
        required = ("admin_id", "luciel_instance_id", "source_id", "limit")
        for name in required:
            self.assertIn(
                name, sig.parameters,
                f"list_recent_traces_using_source missing kw {name!r}",
            )
            self.assertEqual(
                sig.parameters[name].kind,
                inspect.Parameter.KEYWORD_ONLY,
                f"{name!r} must be keyword-only",
            )

    def test_c5_limit_default_is_five(self):
        sig = inspect.signature(
            TraceRepository.list_recent_traces_using_source
        )
        self.assertEqual(
            sig.parameters["limit"].default, 5,
            "limit default must be 5 — Customer Journey §4.3 modal "
            "preview shows up to 5 questions.",
        )

    # ----------------------------------------------------------------
    # SQL shape: build the statement against a synthetic repo and
    # inspect the compiled text.
    # ----------------------------------------------------------------

    def _build_sql(self) -> str:
        # We need to compile the SELECT *without* executing it. The
        # method body builds and passes the statement to
        # ``self.db.scalars(stmt).all()``; we monkey-patch ``scalars``
        # to capture the statement.
        captured: dict = {}

        class _CapturingDb:
            def scalars(self, stmt):
                captured["stmt"] = stmt
                # Return a faux Result whose .all() returns [].
                class _R:
                    def all(self_inner):
                        return []
                return _R()

        repo = TraceRepository(_CapturingDb())  # type: ignore[arg-type]
        repo.list_recent_traces_using_source(
            admin_id="some-admin",
            luciel_instance_id=42,
            source_id=7,
            limit=5,
        )
        self.assertIn("stmt", captured, "method must call self.db.scalars(stmt)")
        return _compiled(captured["stmt"])

    def test_c2_sql_has_three_required_predicates(self):
        sql = self._build_sql()
        # Tenant + instance scope.
        self.assertIn("admin_id", sql)
        self.assertIn("luciel_instance_id", sql)
        # The containment operator (this is the GIN-index path).
        self.assertIn("@>", sql, "Query must use ARRAY containment (@>)")

    def test_c2_sql_filters_on_source_ids_used(self):
        sql = self._build_sql()
        self.assertIn("source_ids_used", sql)

    def test_c3_sql_has_order_by_created_at_desc_and_limit(self):
        sql = self._build_sql()
        # Two predicates we care about — be tolerant of whitespace.
        self.assertRegex(sql, r"ORDER\s+BY\s+traces\.created_at\s+DESC")
        self.assertRegex(sql, r"LIMIT\s+5")

    def test_c4_sql_uses_bigint_array_literal(self):
        """The ARRAY[..] target must be ``::BIGINT[]`` so Postgres
        picks the GIN index plan on ``source_ids_used``. Without the
        cast the planner has to coerce and may fall back to a seq
        scan even though the index exists."""
        sql = self._build_sql()
        # The cast spelling depends on dialect; against the
        # Postgres dialect SQLAlchemy emits ``CAST(... AS BIGINT[])``
        # for ``cast([x], ARRAY(BigInteger))``.
        self.assertRegex(
            sql,
            r"CAST\s*\(\s*ARRAY\s*\[\s*7\s*\]\s+AS\s+BIGINT\s*\[\s*\]\s*\)",
            f"Expected BIGINT[] cast in SQL; got:\n{sql}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
