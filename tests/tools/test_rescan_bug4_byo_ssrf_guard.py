"""RESCAN BUG-4 — SSRF egress guard for the BYO-webhook sandbox.

Manifest: RESCAN_AUDIT_MANIFEST.md TIER A / BUG-4. Architecture §3.8.6
("Requests to private/link-local/metadata IP ranges are blocked at the
network layer ... DNS resolution of the target hostname is validated
against the blocked ranges before the request is issued — a DNS
rebinding attack that resolves a public-looking hostname to a private
IP is blocked").

Before the fix, app/tools/byo/sandbox.py enforced ONLY an FQDN
allowlist; an allowlisted hostname resolving to 169.254.169.254 (AWS
metadata), 10.x, 127.0.0.1, etc. would be dispatched. These tests pin
the IP-range guard and that it fires BEFORE dispatch.

No live network needed: we monkeypatch the resolver (_resolved_ips) to
return controlled addresses, then assert the guard's decision.
"""
from __future__ import annotations

import pytest

from app.tools.byo import sandbox
from app.tools.byo.sandbox import EgressDeniedError, _assert_egress_ip_safe


BLOCKED = [
    ("169.254.169.254", "AWS instance metadata (link-local)"),
    ("169.254.1.1", "link-local"),
    ("10.0.0.5", "RFC1918 10/8"),
    ("172.16.5.5", "RFC1918 172.16/12"),
    ("192.168.1.1", "RFC1918 192.168/16"),
    ("127.0.0.1", "loopback"),
    ("0.0.0.0", "unspecified"),
    ("::1", "IPv6 loopback"),
    ("fe80::1", "IPv6 link-local"),
    ("fd00::1", "IPv6 unique-local (private)"),
]

ALLOWED = [
    "8.8.8.8",        # public (Google DNS)
    "1.1.1.1",        # public (Cloudflare)
    "93.184.216.34",  # public (example.com historical)
]


@pytest.mark.parametrize("ip,label", BLOCKED)
def test_blocked_ip_ranges_are_denied(monkeypatch, ip, label):
    """Every private/link-local/loopback/metadata IP must be rejected,
    even when the hostname itself is allowlisted."""
    monkeypatch.setattr(sandbox, "_resolved_ips", lambda host: [ip])
    with pytest.raises(EgressDeniedError):
        _assert_egress_ip_safe("totally-allowlisted.example.com")


@pytest.mark.parametrize("ip", ALLOWED)
def test_public_ips_pass(monkeypatch, ip):
    """Genuinely public, globally-routable addresses must pass."""
    monkeypatch.setattr(sandbox, "_resolved_ips", lambda host: [ip])
    _assert_egress_ip_safe("api.partner.example.com")  # no raise


def test_dns_rebind_any_blocked_ip_in_set_denies(monkeypatch):
    """DNS-rebind defense: if a host resolves to MULTIPLE addresses and
    ANY one is blocked, the whole host is denied (an attacker cannot mix
    one public + one private answer to slip through)."""
    monkeypatch.setattr(
        sandbox, "_resolved_ips",
        lambda host: ["8.8.8.8", "169.254.169.254"],
    )
    with pytest.raises(EgressDeniedError):
        _assert_egress_ip_safe("rebind.example.com")


def test_unresolvable_host_fails_closed(monkeypatch):
    """A host that does not resolve is denied (fail-closed), not allowed."""
    def _boom(host):
        raise OSError("Name or service not known")
    monkeypatch.setattr(sandbox, "_resolved_ips", _boom)
    with pytest.raises(EgressDeniedError):
        _assert_egress_ip_safe("does-not-exist.invalid")


def test_hard_timeout_is_ten_seconds():
    """Architecture §3.8.6: enforced timeout is 10s (was 30s)."""
    assert sandbox.BYO_HARD_TIMEOUT_SECONDS == 10, (
        "BYO hard timeout must be 10s per Architecture §3.8.6. If this "
        "changes, the architecture doc + §9 item 12 must change with it."
    )
    # child HTTP timeout must fire before the SIGKILL deadline
    assert sandbox._CHILD_REQUEST_TIMEOUT_SECONDS < sandbox.BYO_HARD_TIMEOUT_SECONDS


def test_guard_runs_before_dispatch(monkeypatch):
    """The SSRF guard must short-circuit to egress_denied BEFORE the
    subprocess is spawned, when SPAWN_OVERRIDE is None (production path)."""
    import asyncio

    # Force the production path (no spawn override) and make the resolver
    # return a metadata IP for an allowlisted host.
    monkeypatch.setattr(sandbox, "SPAWN_OVERRIDE", None)
    monkeypatch.setattr(sandbox, "_resolved_ips",
                        lambda host: ["169.254.169.254"])

    # If the guard fails to fire, _spawn_and_collect would run; make it
    # explode so a regression is loud rather than silent.
    async def _boom(*a, **k):
        raise AssertionError("dispatch ran despite SSRF guard")
    monkeypatch.setattr(sandbox, "_spawn_and_collect", _boom)

    from app.tools.byo.circuit_breaker import CircuitBreaker, InMemoryBackend
    breaker = CircuitBreaker(backend=InMemoryBackend())

    env = asyncio.run(sandbox.dispatch_byo_webhook(
        endpoint_id=999,
        endpoint_url="https://allowlisted.example.com/hook",
        payload={},
        endpoint_input_schema={"type": "object"},
        endpoint_output_schema={"type": "object"},
        allowed_domains=["allowlisted.example.com"],
        breaker=breaker,
        retry_count=0,
        sleep_fn=lambda s: asyncio.sleep(0),
    ))
    assert env.success is False
    assert env.error_class == "egress_denied"
    assert env.attempts == 0
