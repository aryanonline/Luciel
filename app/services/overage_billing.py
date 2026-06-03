"""Conversation-overage billing helpers — Arc 18 (§3.4.1b, spec §33-34).

Pure functions for the cycle-close overage computation, shared by the
``invoice.paid`` webhook handler and its tests. No I/O, no Stripe, no DB —
the webhook owns the side effects; this module owns the arithmetic and the
EXACT invoice line-item copy.

Two decisions are locked here and asserted by tests:

  * **Rounding** (spec §34): overage is billed per 100 conversations,
    rounding partial hundreds UP. 201 conversations over a 200 cap = 1
    over = ``ceil(1/100)`` = 1 unit (1 × the per-100 rate). This is the
    standard metered convention and is the conservative (founder-favourable)
    direction; documented in ARC18_BACKEND_REPORT.md.

  * **Invoice line-item format** (spec §33), EXACTLY:
    ``Conversation overage — [Instance name]: Z additional conversations × rate``
    where ``Z`` is the RAW additional-conversation count (not the rounded
    unit count) and ``rate`` is the human rate string (e.g. ``$15.00/100``).
    The em-dash (U+2014) and the multiplication sign (U+00D7) are literal.
"""

from __future__ import annotations

import math

# Conversation-overage billing unit: priced per this many conversations.
OVERAGE_UNIT_SIZE = 100


def overage_count(*, conversations_used: int, budget_cap: int) -> int:
    """Raw additional conversations beyond the cap (never negative)."""
    return max(0, conversations_used - budget_cap)


def overage_units(overage: int) -> int:
    """Billable units = partial hundreds rounded UP (spec §34).

    0 → 0; 1 → 1; 100 → 1; 101 → 2.
    """
    if overage <= 0:
        return 0
    return math.ceil(overage / OVERAGE_UNIT_SIZE)


def rate_string_from_cents(rate_per_100_cents: int) -> str:
    """Render the per-100 rate as a human string, e.g. 1500 → ``$15.00/100``."""
    dollars = rate_per_100_cents / 100
    return f"${dollars:.2f}/100"


def overage_line_item_description(
    *, instance_name: str, additional: int, rate_str: str
) -> str:
    """The EXACT invoice line-item description (spec §33).

    ``additional`` is the RAW additional-conversation count Z (not units).
    Format is locked and asserted by a format-string test:
    ``Conversation overage — [Instance name]: Z additional conversations × rate``
    """
    return (
        f"Conversation overage — {instance_name}: "
        f"{additional} additional conversations × {rate_str}"
    )
