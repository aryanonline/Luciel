"""Identity subsystem — Step 24.5c.

Sibling package to app/memory/, app/knowledge/, app/persona/. Holds the
identity resolver that maps channel-specific identifiers (email, phone,
sso_subject) asserted by ingress adapters back to durable User identities
and resolves the conversation_id that groups sibling sessions across
channels.

Public surface:
    resolve_identity(...)   -- the §3.3 step 4 hook
    normalise_claim_value(...) -- canonicaliser used by both adapters
                                  and the resolver so both sides of
                                  the unique constraint see the same
                                  representation.

See ARCHITECTURE §3.2.11 for the design contract.
"""
from app.identity.resolver import (
    IdentityResolution,
    IdentityResolver,
    normalise_claim_value,
)

__all__ = [
    "IdentityResolution",
    "IdentityResolver",
    "normalise_claim_value",
]
