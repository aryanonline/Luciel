"""get_secret_store — the store selector.

Returns the real AWS store ONLY when
``settings.connections_live_secrets_enabled`` is True; the in-memory
fake otherwise. Mirrors the ``channels_live_provisioning_enabled``
live-switch convention so a mis-wired test can never touch AWS: the
boot-safe default (flag False) yields the fake and never constructs a
boto3 client.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.integrations.secrets.base import SecretStore
from app.integrations.secrets.fake_store import LocalFakeSecretStore

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings


def get_secret_store(settings: "Settings") -> SecretStore:
    """Select the secret store from settings.

    DEPLOY-GATED: the AWS branch is reached only when
    ``connections_live_secrets_enabled`` is flipped True in production.
    """
    if settings.connections_live_secrets_enabled:
        # DEPLOY-GATED: real Secrets Manager store. Requires AWS creds +
        # IAM secretsmanager:* on luciel/connections/*.
        from app.integrations.secrets.aws_store import (
            AwsSecretsManagerStore,
        )

        return AwsSecretsManagerStore(region_name=settings.aws_region)
    return LocalFakeSecretStore()
