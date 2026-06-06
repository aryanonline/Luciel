"""One-time manual OAuth bootstrap — obtain the FIRST Google refresh token.

The live E2E (``tests/integration/test_oauth_e2e_live.py``) can drive the
real token-REFRESH path against Google's endpoint, but the very first
refresh token only comes from an interactive human consent (clicking
"Approve" on Google's screen) — something the headless test cannot do.

This script bridges that gap. Run it once at a real terminal:

    export GOOGLE_OAUTH_CLIENT_ID=...
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    export GOOGLE_OAUTH_REDIRECT_URI=...        # must match the OAuth client
    export AWS_ACCESS_KEY_ID=...                 # only if --store is passed
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_DEFAULT_REGION=ca-central-1
    python scripts/oauth_manual_bootstrap.py --admin-id A --instance-id 1

It:
  1. builds the REAL consent URL (access_type=offline + prompt=consent so
     Google returns a refresh token) and prints it,
  2. waits for you to open it, approve, and paste back the ``code`` from
     the redirect URL,
  3. runs the REAL ``exchange_code`` against Google's token endpoint,
  4. prints the refresh token (and, with ``--store``, writes it to AWS
     Secrets Manager via the real store, printing ONLY the returned ARN
     ref — never the value).

NOTHING here is committed. The printed refresh token is a live secret;
treat it as such. The stored secret name matches the route convention
(``{admin_id}/{instance_id}/{connection_type}`` → store prefixes
``luciel/connections/``) so the gated E2E can resolve the same ref.
"""
from __future__ import annotations

import argparse
import sys

from app.core.config import settings
from app.integrations.oauth import OAuthError, get_oauth_provider
from app.integrations.oauth.state import sign_state


def _secret_name_for(
    *, admin_id: str, instance_id: int, connection_type: str
) -> str:
    return f"{admin_id}/{instance_id}/{connection_type}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-id", required=True)
    parser.add_argument("--instance-id", type=int, required=True)
    parser.add_argument("--connection-type", default="calendar")
    parser.add_argument(
        "--store",
        action="store_true",
        help=(
            "Write the refresh token to AWS Secrets Manager via the real "
            "store (requires connections_live_secrets_enabled + AWS creds)."
        ),
    )
    args = parser.parse_args()

    provider = get_oauth_provider(args.connection_type, settings)
    if provider is None or not provider.is_configured():
        print(
            "OAuth provider is not configured: set "
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
            "GOOGLE_OAUTH_REDIRECT_URI in the environment.",
            file=sys.stderr,
        )
        return 2

    state = sign_state(
        admin_id=args.admin_id,
        instance_id=args.instance_id,
        connection_type=args.connection_type,
        secret=settings.oauth_state_signing_secret,
    )
    auth_url = provider.authorization_url(state=state)

    print("\n1. Open this URL in a browser, approve, then copy the")
    print("   `code` query param from the redirect URL:\n")
    print(auth_url)
    print()
    code = input("2. Paste the authorization code here: ").strip()
    if not code:
        print("No code provided; aborting.", file=sys.stderr)
        return 2

    try:
        tokens = provider.exchange_code(code=code)
    except OAuthError as exc:
        print(f"exchange_code failed: {exc}", file=sys.stderr)
        return 1

    if not tokens.refresh_token:
        print(
            "Google returned NO refresh token. Re-run and ensure the "
            "consent screen prompts fresh (the URL already sets "
            "access_type=offline + prompt=consent); if the app was "
            "previously authorized you may need to revoke it first at "
            "https://myaccount.google.com/permissions",
            file=sys.stderr,
        )
        return 1

    print("\nexchange_code succeeded.")
    print(f"  access_token (truncated): {tokens.access_token[:12]}...")
    print(f"  refresh_token: {tokens.refresh_token}")
    print(f"  scope: {tokens.scope}")
    print(f"  expires_in: {tokens.expires_in}")

    if args.store:
        if not settings.connections_live_secrets_enabled:
            print(
                "\n--store requires connections_live_secrets_enabled=1 so "
                "the real AWS store is selected (otherwise the value lands "
                "in the in-memory fake and is lost on exit).",
                file=sys.stderr,
            )
            return 2
        from app.integrations.secrets import get_secret_store

        store = get_secret_store(settings)
        name = _secret_name_for(
            admin_id=args.admin_id,
            instance_id=args.instance_id,
            connection_type=args.connection_type,
        )
        ref = store.put(name, tokens.refresh_token)
        print(f"\nStored in AWS Secrets Manager. secret_ref (ARN): {ref}")
        print(
            "  (the gated E2E reads this ref to drive the real refresh; "
            "delete it when done — the lifecycle test also exercises delete.)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
