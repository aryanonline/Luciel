"""AwsSecretsManagerStore — the REAL boto3 secret store.

DEPLOY-GATED: every method here makes a live AWS Secrets Manager call
and therefore requires AWS credentials + an IAM ``secretsmanager:*``
grant on the ``luciel/connections/*`` name prefix. It is NEVER selected
by ``get_secret_store`` unless ``connections_live_secrets_enabled`` is
True, and it is NEVER exercised by the test suite (tests use
``LocalFakeSecretStore``). The boto3 client is constructed lazily on
first use so merely importing this module costs nothing and needs no
creds.

secret_ref contract (Locked Decision #18): ``put``/``rotate``
return the secret's ARN, and that ARN string is the ONLY thing the
caller persists in ``instance_connections.secret_ref``. The value
lives solely in Secrets Manager.
"""
from __future__ import annotations

from typing import Any

from app.integrations.secrets.base import SecretStore, SecretStoreError

_NAME_PREFIX = "luciel/connections/"


class AwsSecretsManagerStore(SecretStore):
    """boto3-backed ``SecretStore``. DEPLOY-GATED — see module docstring."""

    def __init__(self, *, region_name: str) -> None:
        self._region_name = region_name
        self._client: Any = None

    def _get_client(self) -> Any:
        # DEPLOY-GATED: constructs a live AWS client. Lazy so import +
        # construction never touch the network; the first real call does.
        if self._client is None:
            import boto3  # local import keeps boto3 off the import path

            self._client = boto3.client(
                "secretsmanager", region_name=self._region_name
            )
        return self._client

    @staticmethod
    def _name_for(name: str) -> str:
        return name if name.startswith(_NAME_PREFIX) else f"{_NAME_PREFIX}{name}"

    def put(self, name: str, value: str) -> str:
        # DEPLOY-GATED: live CreateSecret call.
        from botocore.exceptions import ClientError

        secret_name = self._name_for(name)
        client = self._get_client()
        try:
            resp = client.create_secret(
                Name=secret_name, SecretString=value
            )
            return resp["ARN"]
        except ClientError as exc:  # pragma: no cover - DEPLOY-GATED
            code = exc.response.get("Error", {}).get("Code")
            if code == "ResourceExistsException":
                # Secret already exists → overwrite its value, return ARN.
                return self.rotate(secret_name, value)
            raise SecretStoreError(str(exc)) from exc

    def get(self, ref: str) -> str:
        # DEPLOY-GATED: live GetSecretValue call.
        from botocore.exceptions import ClientError

        client = self._get_client()
        try:
            resp = client.get_secret_value(SecretId=ref)
        except ClientError as exc:  # pragma: no cover - DEPLOY-GATED
            raise SecretStoreError(str(exc)) from exc
        value = resp.get("SecretString")
        if value is None:  # pragma: no cover - DEPLOY-GATED
            raise SecretStoreError(f"secret {ref!r} has no SecretString")
        return value

    def delete(self, ref: str) -> None:
        # DEPLOY-GATED: live DeleteSecret call. Idempotent — a missing
        # secret is not an error (lifecycle cleanup must not fail loud
        # on an already-removed ref).
        from botocore.exceptions import ClientError

        client = self._get_client()
        try:
            client.delete_secret(
                SecretId=ref, ForceDeleteWithoutRecovery=True
            )
        except ClientError as exc:  # pragma: no cover - DEPLOY-GATED
            code = exc.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                return
            raise SecretStoreError(str(exc)) from exc

    def rotate(self, ref: str, value: str) -> str:
        # DEPLOY-GATED: live PutSecretValue call.
        from botocore.exceptions import ClientError

        client = self._get_client()
        try:
            resp = client.put_secret_value(
                SecretId=ref, SecretString=value
            )
            return resp.get("ARN", ref)
        except ClientError as exc:  # pragma: no cover - DEPLOY-GATED
            raise SecretStoreError(str(exc)) from exc
