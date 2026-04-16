"""
Seed platform-wide default retention policies.

Run once after migration:
    python -m scripts.seed_retention_defaults
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from app.models.retention import RetentionPolicy
from app.policy.retention_rules import PLATFORM_DEFAULTS
from app.repositories.retention_repository import RetentionRepository


def seed() -> None:
    db = SessionLocal()
    try:
        repo = RetentionRepository(db)

        for default in PLATFORM_DEFAULTS:
            existing = repo.get_policy_for_category(
                data_category=default["data_category"],
                tenant_id=None,
            )
            if existing:
                print(
                    f"  SKIP  {default['data_category']} — "
                    f"platform default already exists (id={existing.id})"
                )
                continue

            policy = RetentionPolicy(
                tenant_id=None,
                data_category=default["data_category"],
                retention_days=default["retention_days"],
                action=default["action"],
                purpose=default["purpose"],
                created_by="system",
            )
            repo.create_policy(policy)
            print(
                f"  SEED  {default['data_category']} — "
                f"{default['retention_days']} days, {default['action']}"
            )

        print("\nDone. Platform retention defaults are in place.")

    finally:
        db.close()


if __name__ == "__main__":
    seed()