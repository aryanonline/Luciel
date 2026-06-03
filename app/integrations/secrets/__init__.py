"""Secret-store integration package — Arc 17 Connections layer.

A connection's secret (an OAuth refresh token, a webhook signing key)
NEVER lives in Postgres. ``instance_connections.credential_ref`` stores
ONLY a pointer — the secret NAME/ARN — and the value rides behind a
``SecretStore`` (Locked Decision #18).

Three pieces:
  * ``SecretStore``            — the ABC every store satisfies.
  * ``AwsSecretsManagerStore`` — the real boto3 store. DEPLOY-GATED:
                                 constructing/using it requires AWS
                                 creds + an IAM secretsmanager:* grant.
  * ``LocalFakeSecretStore``   — in-memory dict store for dev/CI/tests.

``get_secret_store(settings)`` is the factory: it returns the AWS store
ONLY when ``settings.connections_live_secrets_enabled`` is True, and the
fake otherwise. The default (flag False) means no boto3 client is ever
constructed off the import path — the full connection code path is
exercisable without AWS.
"""
from __future__ import annotations

from app.integrations.secrets.base import (
    SecretStore,
    SecretStoreError,
)
from app.integrations.secrets.aws_store import AwsSecretsManagerStore
from app.integrations.secrets.fake_store import LocalFakeSecretStore
from app.integrations.secrets.factory import get_secret_store

__all__ = [
    "SecretStore",
    "SecretStoreError",
    "AwsSecretsManagerStore",
    "LocalFakeSecretStore",
    "get_secret_store",
]
