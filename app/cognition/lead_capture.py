"""Arc 14 U4 — §3.4.4 lead-threshold detection + extraction.

Lead capture is always-on cognition (§3.4): every Luciel, every tier,
NOT a tool, NOT admin-configurable. This module decides WHETHER a
conversation has crossed the lead threshold and, if so, extracts the
structured lead fields the dashboard lead view needs.

Threshold (§3.4.4) — fires when the customer signals sales-qualified by
ANY of:
  * gave CONTACT INFO (email or phone in the conversation);
  * mentioned a BUDGET (a money amount);
  * asked about a SPECIFIC LISTING WITH INTENT (a listing/address
    reference paired with a viewing / buying / pricing intent verb).
It does NOT fire on idle chit-chat ("hi", "thanks", "what's the
weather").

Why deterministic
-----------------
The §3.4.5 escalation classifiers are LLM-backed (intent / sentiment
are fuzzy). Lead-threshold detection here is deterministic regex/keyword
extraction so it is:
  * hermetic + free in tests (no LLM call, no network — founder #2);
  * a stable, assertable boundary (a test can pin "this message crosses,
    that one does not");
  * cheap to run on EVERY turn (it is always-on cognition).
A richer LLM-backed extractor is a later hook — it can replace
``detect`` without touching the finalizer or the lead row shape.

Pure decision: this module reads text and returns a ``LeadCandidate``
(or ``None``). It never touches the DB — persistence is the finalizer's
job (mirrors the judge/service split in §3.4.5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# --- Detection patterns (deterministic, case-insensitive) ---

# Email + phone — contact info. A phone is >= 10 digits allowing spaces,
# dashes, parens, leading +.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s\-().]*){10,}(?!\d)")

# Budget — a money amount: $750,000 / 750k / 1.2m / "budget of 500000".
_MONEY_RE = re.compile(
    r"""
    (?:\$\s?)?                       # optional leading $
    (\d{1,3}(?:[,\s]\d{3})+          # 750,000 / 1 200 000
       |\d+(?:\.\d+)?)               # 750000 / 1.2
    \s?(k|m|thousand|million)?       # optional magnitude suffix
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BUDGET_CONTEXT_RE = re.compile(
    r"\b(budget|afford|spend|price\s*range|up\s*to|around|under|below|"
    r"looking\s*to\s*spend|\$)\b",
    re.IGNORECASE,
)

# Specific-listing intent — a listing/address reference paired with a
# buy/view/price verb.
_LISTING_REF_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z][A-Za-z ]+\b"          # "123 Main St"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|"
    r"court|ct|place|pl|way|terrace)\b"
    r"|listing\s*#?\s*\w+"                          # "listing #4567"
    r"|MLS\s*#?\s*\w+"                              # "MLS 12345"
    r"|property\s*#?\s*\w+)",                       # "property 88"
    re.IGNORECASE,
)
_INTENT_VERB_RE = re.compile(
    r"\b(view|viewing|tour|see|visit|buy|buying|purchase|offer|"
    r"schedule|book|interested\s*in|price\s*of|how\s*much|available)\b",
    re.IGNORECASE,
)

# Magnitudes for normalising a money match to an absolute value.
_MAGNITUDE = {
    "k": 1_000.0,
    "thousand": 1_000.0,
    "m": 1_000_000.0,
    "million": 1_000_000.0,
}


@dataclass
class LeadCandidate:
    """A conversation that crossed the §3.4.4 lead threshold.

    Carries the structured fields the dashboard lead row needs. Fields
    are best-effort — a lead crosses on ANY one qualifying signal, so not
    every field is always populated. ``lead_value`` is the extracted
    budget (when a budget triggered / accompanied the threshold), surfaced
    so the §3.4.5 high-value-lead OUTCOME signal can read the SAME value
    the lead row recorded (one extraction, two consumers).
    """

    triggers: list[str] = field(default_factory=list)
    name: str | None = None
    contact_channel: str | None = None
    contact_identifier: str | None = None
    intent: str | None = None
    key_facts: list[str] = field(default_factory=list)
    next_step: str | None = None
    lead_value: float | None = None


def detect(
    *,
    message: str,
    prior_customer_messages: list[str] | None = None,
    inbound_channel: str | None = None,
) -> LeadCandidate | None:
    """Decide whether the conversation crossed the lead threshold.

    Evaluates the current ``message`` plus any ``prior_customer_messages``
    (the conversation's customer turns, so a budget mentioned earlier and
    contact info given now both count). Returns a populated
    ``LeadCandidate`` when ANY qualifying signal fires, else ``None``.

    Never raises — a detection failure degrades to ``None`` (no lead)
    rather than crashing the turn.
    """
    try:
        customer_text = list(prior_customer_messages or []) + [message or ""]
        blob = "\n".join(t for t in customer_text if t)

        candidate = LeadCandidate()

        # 1. Contact info (email / phone).
        email = _EMAIL_RE.search(blob)
        phone = _PHONE_RE.search(blob)
        if email:
            candidate.triggers.append("contact_info")
            candidate.contact_channel = "email"
            candidate.contact_identifier = email.group(0)
            candidate.key_facts.append(f"email: {email.group(0)}")
        elif phone:
            candidate.triggers.append("contact_info")
            candidate.contact_channel = "sms"
            candidate.contact_identifier = phone.group(0).strip()
            candidate.key_facts.append(f"phone: {phone.group(0).strip()}")

        # 2. Budget — a money amount in a budget context.
        budget = _extract_budget(blob)
        if budget is not None:
            candidate.triggers.append("budget")
            candidate.lead_value = budget
            candidate.key_facts.append(f"budget: {budget:.0f}")

        # 3. Specific-listing intent — listing reference + intent verb.
        listing = _LISTING_REF_RE.search(blob)
        if listing and _INTENT_VERB_RE.search(blob):
            candidate.triggers.append("listing_intent")
            ref = listing.group(0).strip()
            candidate.intent = f"interested in {ref}"
            candidate.key_facts.append(f"listing: {ref}")

        if not candidate.triggers:
            return None

        # Default a coarse channel + next step so the row is actionable.
        if candidate.contact_channel is None and inbound_channel:
            candidate.contact_channel = inbound_channel
        if candidate.next_step is None:
            candidate.next_step = _default_next_step(candidate)
        if candidate.intent is None:
            candidate.intent = "sales-qualified conversation"
        return candidate
    except Exception:  # noqa: BLE001 — never crash the turn over detection
        return None


def _extract_budget(blob: str) -> float | None:
    """Return the largest money amount mentioned in a budget context.

    Requires a budget-context cue ($ sign, "budget", "spend", etc.) so a
    bare number ("I have 2 kids") does not register as a budget.
    """
    if not _BUDGET_CONTEXT_RE.search(blob):
        return None
    best: float | None = None
    for m in _MONEY_RE.finditer(blob):
        raw, suffix = m.group(1), (m.group(2) or "").lower()
        digits = raw.replace(",", "").replace(" ", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        value *= _MAGNITUDE.get(suffix, 1.0)
        # Ignore implausibly small "amounts" that are almost certainly not
        # a real-estate budget (e.g. a street number or a count).
        if value < 1_000:
            continue
        if best is None or value > best:
            best = value
    return best


def _default_next_step(candidate: LeadCandidate) -> str:
    if "listing_intent" in candidate.triggers:
        return "schedule a viewing"
    if "contact_info" in candidate.triggers:
        return "follow up with the customer"
    if "budget" in candidate.triggers:
        return "share matching listings"
    return "follow up with the customer"
