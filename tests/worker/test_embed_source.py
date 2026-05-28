"""Arc 11 Step 6 — embed_source task contract tests.

Project convention for worker-task tests is static AST + module-load
checks (see ``tests/api/test_step29y_cluster4_worker_hardening.py``
for the precedent on ``memory_extraction``). The reasons:

  * The task's correctness depends on the DECORATOR config
    (autoretry_for, acks_late, queue), which is AST-readable but
    doesn't reproduce reliably in a unit test that doesn't have a
    broker.
  * The PII / logging / RLS-binding-order disciplines are also
    code-shape facts, not behaviour — easier to lock with AST.
  * The behavioural happy path (S3 → parse → embed → persist) needs
    boto3 + pgvector + a real LLM credential to exercise end-to-end;
    that lives behind ``LUCIEL_LIVE_POSTGRES_URL`` (none of those
    pieces have unit-test surface that survives without
    aggressive mocking that would shadow the actual code path).

Contracts guarded here:

  C1   Task module imports cleanly.
  C2   Decorator config (name, queue, max_retries, autoretry_for,
       retry_backoff*, acks_late, bind=True).
  C3   Function signature: kwargs ``source_pk``, ``admin_id``,
       ``instance_id`` only; ``self`` first; all keyword-only.
  C4   Exception taxonomy: ``TransientIngestionError``,
       ``IngestionPermanentError``, ``IngestionConfigError`` (the
       last a subclass of the second); ``_TRANSIENT_EXC`` includes
       the first one and ``OperationalError`` + redis
       ``ConnectionError``.
  C5   Idempotency: when a source is already in ``'ready'`` state
       the task returns without calling the embedder or the
       persistence path.
  C6   RLS discipline: ``bind_tenant_scope`` is entered BEFORE
       ``SessionLocal()`` is opened. Verified by inspecting the AST.
  C7   PII discipline: the source code does NOT log
       ``source.filename``, ``source.s3_key``, chunk content, or
       any other potentially-sensitive string. Logger calls are
       restricted to opaque ids + counts + exception class names.
  C8   The task's failure handler writes to
       ``KnowledgeSourceRepository.mark_status(..., status='failed',
       error=...)``.
"""
from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path

from app.worker.tasks import embed_source as embed_source_module
from app.worker.tasks.embed_source import (
    IngestionConfigError,
    IngestionPermanentError,
    TransientIngestionError,
    _TRANSIENT_EXC,
    embed_source,
)


_SRC_PATH = Path(embed_source_module.__file__)
_SRC = _SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# C1 — module imports cleanly.
# ---------------------------------------------------------------------


class TestC1Imports(unittest.TestCase):

    def test_task_module_importable(self):
        # The import at the top of THIS file would have raised if the
        # module was broken; the assertion is a guard for readers
        # scanning the test names.
        self.assertTrue(callable(embed_source))


# ---------------------------------------------------------------------
# C2 — decorator config.
# ---------------------------------------------------------------------


class TestC2DecoratorConfig(unittest.TestCase):

    def test_task_registered_name_is_fully_qualified(self):
        self.assertEqual(
            embed_source.name,
            "app.worker.tasks.embed_source.embed_source",
        )

    def test_task_queue_is_knowledge_tasks(self):
        # Celery stashes decorator-level options on the task class.
        self.assertEqual(
            getattr(embed_source, "queue", None),
            "luciel-knowledge-tasks",
        )

    def test_task_max_retries(self):
        self.assertEqual(getattr(embed_source, "max_retries", None), 3)

    def test_task_acks_late(self):
        self.assertTrue(getattr(embed_source, "acks_late", None))

    def test_task_ignore_result(self):
        self.assertTrue(getattr(embed_source, "ignore_result", None))

    def test_autoretry_for_includes_transient_exc(self):
        autoretry = getattr(embed_source, "autoretry_for", None)
        # autoretry_for may be a tuple of classes; assert it's
        # non-empty and contains our transient class.
        self.assertIsNotNone(autoretry)
        self.assertIn(TransientIngestionError, autoretry)

    def test_bind_true(self):
        """``bind=True`` is required for autoretry's self.retry() to
        work. Celery strips ``self`` from the public ``.run`` signature
        so we verify via AST against the underlying ``def`` instead."""
        tree = ast.parse(_SRC)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "embed_source"
        )
        first_arg = fn.args.args[0] if fn.args.args else None
        self.assertIsNotNone(first_arg)
        self.assertEqual(
            first_arg.arg, "self",
            "bind=True decorator: first positional arg of def must be 'self'",
        )


# ---------------------------------------------------------------------
# C3 — function signature.
# ---------------------------------------------------------------------


class TestC3Signature(unittest.TestCase):

    def _embed_source_fn(self) -> ast.FunctionDef:
        tree = ast.parse(_SRC)
        return next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "embed_source"
        )

    def test_kwargs_are_keyword_only(self):
        fn = self._embed_source_fn()
        kwonly = {a.arg for a in fn.args.kwonlyargs}
        for name in ("source_pk", "admin_id", "instance_id"):
            self.assertIn(
                name, kwonly,
                f"{name} must be a keyword-only arg on embed_source",
            )

    def test_no_extra_kwargs_in_payload(self):
        """The opaque-ids-only doctrine from memory_extraction.py
        applies here too: no filename, no content, no s3_key in
        the payload — every additional kwarg is a PII vector."""
        fn = self._embed_source_fn()
        # positional-or-keyword + keyword-only + positional only:
        # we expect ``self`` as the only positional arg, and the
        # three keyword-only kwargs.
        positional = {a.arg for a in fn.args.args}
        kwonly = {a.arg for a in fn.args.kwonlyargs}
        self.assertEqual(positional, {"self"})
        self.assertEqual(
            kwonly,
            {"source_pk", "admin_id", "instance_id"},
            "Every new kwarg is a potential PII vector — review "
            "before adding.",
        )


# ---------------------------------------------------------------------
# C4 — exception taxonomy.
# ---------------------------------------------------------------------


class TestC4ExceptionTaxonomy(unittest.TestCase):

    def test_transient_class_is_distinct_from_permanent(self):
        self.assertFalse(issubclass(TransientIngestionError,
                                    IngestionPermanentError))
        self.assertFalse(issubclass(IngestionPermanentError,
                                    TransientIngestionError))

    def test_config_error_is_permanent(self):
        self.assertTrue(issubclass(IngestionConfigError,
                                   IngestionPermanentError))

    def test_transient_tuple_membership(self):
        # Must include our explicit class.
        self.assertIn(TransientIngestionError, _TRANSIENT_EXC)
        # SQLAlchemy OperationalError is transient (DB connect blip).
        from sqlalchemy.exc import OperationalError
        self.assertIn(OperationalError, _TRANSIENT_EXC)
        # Redis ConnectionError covers broker network blips.
        import redis.exceptions
        self.assertIn(redis.exceptions.ConnectionError, _TRANSIENT_EXC)


# ---------------------------------------------------------------------
# C5 — idempotency: 'ready' state -> no-op.
# ---------------------------------------------------------------------


class TestC5Idempotency(unittest.TestCase):
    """Verify the task body short-circuits when the source is
    already 'ready'. AST inspection of the source — the check
    must precede the parse / embed / persist work."""

    def test_ready_short_circuit_present(self):
        # Look for the idempotent ready-state branch + a return.
        idempotent_block = re.search(
            r"if\s+source\.ingestion_status\s*==\s*['\"]ready['\"]\s*:"
            r".*?return",
            _SRC,
            re.DOTALL,
        )
        self.assertIsNotNone(
            idempotent_block,
            "embed_source must short-circuit when source.ingestion_status "
            "== 'ready'. At-least-once delivery + DLQ redrive both rely on "
            "this for non-duplicating behaviour.",
        )

    def test_ready_check_before_processing_flip(self):
        ready_at = _SRC.find('source.ingestion_status == "ready"')
        processing_at = _SRC.find('status="processing"')
        self.assertNotEqual(ready_at, -1)
        self.assertNotEqual(processing_at, -1)
        self.assertLess(
            ready_at, processing_at,
            "The 'ready' idempotency check must run BEFORE the "
            "'processing' flip; otherwise an already-ready source "
            "would briefly transition back to processing on each retry.",
        )

    def test_missing_source_returns_noop(self):
        # AST check: ``if source is None: ... return`` exists.
        m = re.search(
            r"if\s+source\s+is\s+None\s*:.*?return",
            _SRC,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "embed_source must return without raising when the source "
            "row was deleted between enqueue and execution.",
        )


# ---------------------------------------------------------------------
# C6 — RLS discipline: bind_tenant_scope BEFORE SessionLocal().
# ---------------------------------------------------------------------


class TestC6RlsDiscipline(unittest.TestCase):
    """Arc 9 C4.4: scope ContextVars MUST be bound before the DB
    session opens. Opening SessionLocal() outside the bind_tenant_scope
    with-block would emit empty GUCs on the first BEGIN that linger on
    the pooled connection."""

    def test_bind_tenant_scope_appears_before_session_local(self):
        bind_at = _SRC.find("with bind_tenant_scope(")
        session_at = _SRC.find("db = SessionLocal()")
        self.assertNotEqual(bind_at, -1, "must use bind_tenant_scope")
        self.assertNotEqual(session_at, -1, "must open SessionLocal()")
        self.assertLess(
            bind_at, session_at,
            "bind_tenant_scope(...) must appear BEFORE SessionLocal(). "
            "Arc 9 C4.4 / ARC11_PLAN.md §13 Step 4 carry-forward.",
        )

    def test_session_local_inside_with_block(self):
        """The SessionLocal() must be inside the with-block — opening
        it outside emits empty GUCs on the lazy BEGIN."""
        # Find the function body and inspect the AST.
        tree = ast.parse(_SRC)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "embed_source"
        )
        # Locate the ``with bind_tenant_scope(...)`` ``With`` node and
        # assert SessionLocal() appears as one of its body statements.
        with_node = next(
            (n for n in ast.walk(fn) if isinstance(n, ast.With)),
            None,
        )
        self.assertIsNotNone(with_node)
        body_src = "\n".join(ast.unparse(stmt) for stmt in with_node.body)
        self.assertIn("SessionLocal()", body_src,
                      "SessionLocal() must be opened inside the "
                      "with bind_tenant_scope(...) block.")

    def test_does_not_use_ops_session_local(self):
        """``OpsSessionLocal`` / ``luciel_ops`` / BYPASSRLS is the
        wrong primitive for per-tenant work; this task must not
        reach for it. Inspect AST imports + Call nodes — the
        docstring is allowed to *mention* OpsSessionLocal as a
        "do not use" reference."""
        tree = ast.parse(_SRC)
        for node in ast.walk(tree):
            # Imports of OpsSessionLocal.
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                self.assertNotIn(
                    "OpsSessionLocal", names,
                    "embed_source must not import OpsSessionLocal — "
                    "BYPASSRLS defeats the tenant fence.",
                )
            # Calls to OpsSessionLocal.
            if isinstance(node, ast.Call):
                func_name = (
                    node.func.id if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", None)
                )
                self.assertNotEqual(
                    func_name, "OpsSessionLocal",
                    "embed_source must not call OpsSessionLocal — "
                    "BYPASSRLS defeats the tenant fence.",
                )


# ---------------------------------------------------------------------
# C7 — PII discipline: no filename / s3_key / chunk content in logs.
# ---------------------------------------------------------------------


class TestC7PiiDiscipline(unittest.TestCase):

    def _logger_calls_in_source(self) -> list[str]:
        """Return the source text of every ``logger.<level>(...)`` call."""
        calls: list[str] = []
        tree = ast.parse(_SRC)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            ):
                calls.append(ast.unparse(node))
        return calls

    def test_no_logger_call_passes_filename(self):
        for call in self._logger_calls_in_source():
            self.assertNotIn(
                "source.filename", call,
                f"logger call leaks filename: {call!r}",
            )
            self.assertNotIn(
                "filename_hint", call,
                f"logger call leaks filename hint: {call!r}",
            )

    def test_no_logger_call_passes_s3_key(self):
        for call in self._logger_calls_in_source():
            self.assertNotIn(
                "source.s3_key", call,
                f"logger call leaks s3_key: {call!r}",
            )
            self.assertNotIn(
                "s3_key", call,
                f"logger call references s3_key parameter: {call!r}",
            )

    def test_no_logger_call_passes_chunk_content(self):
        for call in self._logger_calls_in_source():
            # Catches raw parsed text + chunk indexing. ``len(chunks)``
            # and ``chunk_count=%d`` are fine — those are counts, not
            # content.
            self.assertNotIn(
                "parsed.text", call,
                f"logger call leaks parsed text: {call!r}",
            )
            self.assertNotIn(
                "chunks[", call,
                f"logger call leaks chunk text via indexing: {call!r}",
            )
            self.assertNotIn(
                "content", call,
                f"logger call references chunk content: {call!r}",
            )
            self.assertNotIn(
                "text=", call,
                f"logger call leaks raw text: {call!r}",
            )

    def test_admin_id_is_truncated_to_prefix_in_log(self):
        # The _log_prefix helper truncates admin_id; logger calls
        # should consume the prefix rather than the full id.
        self.assertIn("_log_prefix(", _SRC)
        # Verify the prefix-builder truncates to 8 chars.
        m = re.search(
            r"def _log_prefix\(admin_id:[^\)]+\)[^\n]+\n(?:\s+[^\n]+\n)+?"
            r"\s+aid_prefix = \(admin_id or \"\"\)\[:8\]",
            _SRC,
        )
        self.assertIsNotNone(
            m, "_log_prefix must truncate admin_id to first 8 chars",
        )

    def test_sanitised_error_does_not_use_str_exc(self):
        """The _sanitise_error helper uses repr(exc) not str(exc) —
        repr keeps the class name visible; str surfaces caller-
        supplied content."""
        m = re.search(
            r"def _sanitise_error\(exc:[^\)]+\)[^\n]+\n(?:\s+[^\n]+\n)+?"
            r"\s+body = repr\(exc\)",
            _SRC,
        )
        self.assertIsNotNone(
            m, "_sanitise_error must use repr(exc), not str(exc)",
        )


# ---------------------------------------------------------------------
# C8 — failure handler writes 'failed' status.
# ---------------------------------------------------------------------


class TestC8FailureHandler(unittest.TestCase):

    def test_mark_failed_swallow_calls_mark_status_failed(self):
        # AST: find the function _mark_failed_swallow and assert it
        # calls mark_status with status="failed".
        tree = ast.parse(_SRC)
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)
             and n.name == "_mark_failed_swallow"),
            None,
        )
        self.assertIsNotNone(
            fn, "task module must define _mark_failed_swallow helper",
        )
        body_src = ast.unparse(fn)
        # ast.unparse renders string literals with single quotes,
        # so accept either spelling.
        self.assertTrue(
            'status="failed"' in body_src or "status='failed'" in body_src,
            f"_mark_failed_swallow must pass status='failed'; got:\n{body_src}",
        )
        self.assertIn("error=_sanitise_error", body_src)

    def test_failure_handler_called_on_transient_and_permanent_paths(self):
        """Both except-blocks must call _mark_failed_swallow."""
        self.assertGreaterEqual(
            _SRC.count("_mark_failed_swallow("), 3,
            "Failure handler must be called from at least the three "
            "except blocks (transient + permanent + catch-all).",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
