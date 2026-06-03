"""LIVE OAuth + Secrets-Manager end-to-end — GATED behind RUN_LIVE_OAUTH_E2E.

This module exercises the REAL wire: live AWS Secrets Manager calls and
live Google token-endpoint calls. It is OPT-IN only — every test skips
unless ``RUN_LIVE_OAUTH_E2E=1`` is set AND the relevant credentials are
present in the environment. Normal CI (no flag, no creds) collects these
tests and skips them, so the standard suite stays green and AWS/Google
are never touched without an operator explicitly opting in.

Run it for real with::

    export RUN_LIVE_OAUTH_E2E=1
    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=ca-central-1
    export GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=...
    export GOOGLE_OAUTH_REDIRECT_URI=...                 # must match the OAuth client
    # optional, for the real refresh sub-part (obtained via
    # scripts/oauth_manual_bootstrap.py --store):
    export LIVE_OAUTH_REFRESH_REF=arn:aws:secretsmanager:...
    pytest tests/integration/test_oauth_e2e_live.py -v -s

What is verified LIVE here:
  * (a) AwsSecretsManagerStore put → get → rotate → delete round-trip,
        asserting pointer-only (ref is an ARN; the value is what we get
        back, never persisted in the ref).
  * Google authorization_url construction (real client id, real consent
    endpoint) — a pure-string build, no network.
  * Negative exchange: a deliberately bad auth code → real Google 4xx →
    honest OAuthError (proves exchange_code reaches the wire).
  * (b) Real Google token REFRESH — SKIPPED with an explicit message when
        no bootstrapped refresh token is available (the one human-consent
        touch the no-deployment constraint forces); RUN when
        LIVE_OAUTH_REFRESH_REF points at a stored refresh token.
  * (c) Lifecycle secret cleanup: put a secret via the REAL store, then
        delete it (the drain worker's operation) and assert it is GONE
        (get raises — ResourceNotFound surfaced as SecretStoreError).
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

RUN_LIVE = os.getenv("RUN_LIVE_OAUTH_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="live OAuth/AWS E2E is opt-in: set RUN_LIVE_OAUTH_E2E=1 with creds",
)


def _have_aws() -> bool:
    return bool(
        os.getenv("AWS_ACCESS_KEY_ID")
        and os.getenv("AWS_SECRET_ACCESS_KEY")
    )


def _have_google() -> bool:
    return bool(
        os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def _live_settings():
    """A Settings instance with the live secret store gate flipped on.

    Reading from a fresh Settings() picks up the exported env creds; we
    force ``connections_live_secrets_enabled=True`` so get_secret_store
    selects the REAL AwsSecretsManagerStore (the boot-safe default is the
    in-memory fake).
    """
    from app.core.config import Settings

    return Settings(connections_live_secrets_enabled=True)


# =====================================================================
# (a) REAL AWS Secrets Manager round-trip — put/get/rotate/delete.
# =====================================================================


@pytest.mark.skipif(not _have_aws(), reason="AWS creds absent")
def test_aws_secrets_manager_real_round_trip():
    from app.integrations.secrets import get_secret_store
    from app.integrations.secrets.aws_store import AwsSecretsManagerStore

    store = get_secret_store(_live_settings())
    assert isinstance(store, AwsSecretsManagerStore), (
        "live gate must select the real AWS store"
    )

    # Unique name so concurrent / repeat runs never collide. The store
    # prefixes luciel/connections/, so the on-AWS name is namespaced.
    name = f"e2e-test/{uuid.uuid4().hex}"
    value = f"refresh-token-{uuid.uuid4().hex}"

    ref = None
    try:
        ref = store.put(name, value)
        # Pointer-only: the ref is an ARN string, NOT the secret value.
        assert ref != value
        assert "arn:aws:secretsmanager" in ref
        assert value not in ref

        # get resolves the ref back to the exact value we stored.
        assert store.get(ref) == value

        # rotate overwrites the value behind the same ref.
        new_value = f"rotated-{uuid.uuid4().hex}"
        rotated_ref = store.rotate(ref, new_value)
        assert store.get(rotated_ref) == new_value
    finally:
        if ref is not None:
            store.delete(ref)

    # After delete the secret is gone — get must now fail loud.
    from app.integrations.secrets.base import SecretStoreError

    with pytest.raises(SecretStoreError):
        store.get(ref)


# =====================================================================
# Google authorization_url — real client id, pure-string build.
# =====================================================================


@pytest.mark.skipif(not _have_google(), reason="Google creds absent")
def test_google_authorization_url_real_build():
    from app.core.config import Settings
    from app.integrations.oauth import get_oauth_provider, sign_state

    settings = Settings()
    provider = get_oauth_provider("calendar", settings)
    assert provider is not None
    assert provider.is_configured(), "Google client creds must be present"

    state = sign_state(
        admin_id="e2e-admin",
        instance_id=1,
        connection_type="calendar",
        secret=settings.oauth_state_signing_secret,
    )
    url = provider.authorization_url(state=state)

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert settings.google_oauth_client_id in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert f"state={state}".split("=")[0] in url  # state param present
    assert state in url


# =====================================================================
# Negative exchange — bad code → real Google 4xx → honest OAuthError.
# =====================================================================


@pytest.mark.skipif(not _have_google(), reason="Google creds absent")
def test_google_exchange_bad_code_real_rejection():
    from app.core.config import Settings
    from app.integrations.oauth import OAuthError, get_oauth_provider

    provider = get_oauth_provider("calendar", Settings())
    assert provider is not None and provider.is_configured()

    # A deliberately invalid auth code. Google's token endpoint rejects
    # it with a 4xx — the provider surfaces that as an honest OAuthError,
    # never a fabricated success.
    with pytest.raises(OAuthError):
        provider.exchange_code(code=f"invalid-code-{uuid.uuid4().hex}")


# =====================================================================
# (b) REAL Google token REFRESH — needs a bootstrapped refresh token.
# =====================================================================


@pytest.mark.skipif(not _have_google(), reason="Google creds absent")
def test_google_token_refresh_real():
    """Drive the REAL refresh against Google using a stored refresh token.

    The first refresh token requires a one-time human browser consent
    (no deployed callback to receive the redirect automatically). Obtain
    it once via ``scripts/oauth_manual_bootstrap.py --store`` and export
    ``LIVE_OAUTH_REFRESH_REF`` pointing at the stored secret (or
    ``LIVE_OAUTH_REFRESH_TOKEN`` with the raw value). Absent either, this
    sub-part SKIPS with an explicit message — it is NEVER faked.
    """
    from app.core.config import Settings
    from app.integrations.oauth import get_oauth_provider

    settings = Settings()
    refresh_token = os.getenv("LIVE_OAUTH_REFRESH_TOKEN")
    ref = os.getenv("LIVE_OAUTH_REFRESH_REF")

    if not refresh_token and ref:
        if not _have_aws():
            pytest.skip(
                "LIVE_OAUTH_REFRESH_REF set but AWS creds absent to resolve it"
            )
        store = get_secret_store_live()
        refresh_token = store.get(ref)

    if not refresh_token:
        pytest.skip(
            "No bootstrapped refresh token: run "
            "scripts/oauth_manual_bootstrap.py --store once (the one human "
            "consent the no-deployment constraint forces), then export "
            "LIVE_OAUTH_REFRESH_REF or LIVE_OAUTH_REFRESH_TOKEN."
        )

    provider = get_oauth_provider("calendar", settings)
    assert provider is not None and provider.is_configured()

    tokens = provider.refresh(refresh_token=refresh_token)
    assert tokens.access_token, "refresh must return a fresh access token"
    assert tokens.expires_in > 0


def get_secret_store_live():
    from app.integrations.secrets import get_secret_store

    return get_secret_store(_live_settings())


# =====================================================================
# (c) Lifecycle secret cleanup — real put then real delete → gone.
# =====================================================================


@pytest.mark.skipif(not _have_aws(), reason="AWS creds absent")
def test_lifecycle_secret_cleanup_real_delete():
    """Simulate the revoke → drain step against REAL AWS.

    The disconnect endpoint enqueues a cleanup row carrying the secret
    POINTER; the drain worker calls ``SecretStore.delete(ref)``. Here we
    drive the same store operation directly: store a secret, delete it via
    the pointer, and assert it is gone from AWS (get raises).
    """
    from app.integrations.secrets import get_secret_store
    from app.integrations.secrets.base import SecretStoreError

    store = get_secret_store(_live_settings())

    name = f"e2e-lifecycle/{uuid.uuid4().hex}"
    ref = store.put(name, f"token-{uuid.uuid4().hex}")
    assert "arn:aws:secretsmanager" in ref

    # The drain worker's operation — delete the pointer.
    store.delete(ref)
    # ForceDeleteWithoutRecovery is immediate, but allow a brief settle.
    time.sleep(1)

    with pytest.raises(SecretStoreError):
        store.get(ref)

    # Idempotent: a second delete on an absent ref is a no-op (no raise).
    store.delete(ref)
