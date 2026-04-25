"""
Celery task registry for the Luciel worker.

Task modules are auto-discovered via the `include` list in
`app.worker.celery_app.celery_app`. Adding a new task module =
add it to that `include` list AND import it here.
"""
from app.worker.tasks import memory_extraction  # noqa: F401

__all__ = ["memory_extraction"]