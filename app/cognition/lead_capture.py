"""Arc 14 U4 — §3.4.4 lead-threshold detection + extraction.
§3.4.5 weighted composite lead-scoring (RESCAN TIER-C refactor).

Lead capture is always-on cognition (§3.4): every Luciel, every tier,
NOT a tool, NOT admin-configurable. This module decides WHETHER a
conversation has crossed the lead threshold and, if so, extracts the
structured lead fields the dashboard lead view needs.

Threshold (§3.4.4) — fires when the customer signals sales-qualified by
ANY of:
  * gave CONTACT INFO (email or phone in the conversation);
  * mentioned a BUDGET (a money amount in a budget context);
  * showed TIME CONSTRAINT (urgency language: "this week", "today", etc.);
  * expressed explicit PURCHASE / BOOKING INTENT (buying, booking, hiring).

It does NOT fire on idle chit-chat ("hi", "thanks", "what's the
weather").

These four signals are FIXED and NON-ADMIN-CONFIGURABLE — which signals
exist is runtime cognition, not contact configuration.

Domain-agnostic design
-----------------------
The previous implementation was real-estate-biased (_LISTING_REF_RE
matching street/avenue/MLS/property references). This module replaces
that with industry-agnostic intent/urgency/budget detection so the
detector serves all verticals (medical-spa bookings, legal consulting,
recruiting, home services, etc.) equally.

Weighted composite score (§3.4.5)
-----------------------------------
  explicit budget figure          weight 0.5
  time-constrained decision       weight 0.3
  explicit purchase/booking intent weight 0.4
  capped at 1.0, normalized to [0, 1]

The contact-info trigger crosses the lead threshold (fires the row) but
does not add weight directly — it is a threshold qualifier, not a scored
signal (the scoring captures VALUE signals).

The normalized score is stored as ``lead_score`` on the ``LeadCandidate``
and emitted as ``signal_confidence`` on the escalation event.

Pro/Enterprise business-context extension hook
-----------------------------------------------
Per §3.4.5: "Pro and Enterprise admins can define custom value rules in
the business-context field; the escalation judgment module incorporates
context + those rules into its scoring logic."

The hook is applied in ``score_lead`` (called by the judge):
``business_context_rules`` is a list of dicts, each with:
  {"pattern": "<regex>", "weight_boost": <float>}
Matching any rule in the context adds its ``weight_boost`` to the raw
sum (before capping to 1.0). Free tier: no context rules (empty list),
so the built-in heuristic applies verbatim.

Currency/locale tolerance
--------------------------
Supported formats (documented limits):
  * Prefix symbols: $, €, £, ¥, ₹ (others not currently detected but
    the BUDGET_CONTEXT_RE "budget"/"spend" path catches them)
  * Suffixes: k/K, m/M, thousand, million (e.g. "5k", "1.5m")
  * Comma/space thousands: "5,000", "1 200 000"
  * Plain decimals: "5000", "1500.00"
  * NOT detected: written amounts without currency ("five thousand
    dollars" — text numerals are outside scope), CNY/JPY numeric formats.
A $100 floor filters street numbers and trivially small amounts.
(RESCAN TIER-C: lowered from $1,000 real-estate bias — spa/legal budgets
start at ~$100-$900.)

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


# ---------------------------------------------------------------------------
# Detection patterns (deterministic, case-insensitive)
# ---------------------------------------------------------------------------

# Email + phone — contact info. A phone is >= 10 digits allowing spaces,
# dashes, parens, leading +.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s\-().]*){10,}(?!\d)")

# Budget — currency-tolerant money amount.
# Supported prefix symbols: $, €, £, ¥, ₹  (locale-tolerant — the
# budget-context cue OR a leading currency symbol anchors detection).
_CURRENCY_SYMBOL_RE = re.compile(r"[$€£¥₹]")

_MONEY_RE = re.compile(
    r"""
    (?:[$€£¥₹]\s*)?                    # optional leading currency symbol
    (\d{1,3}(?:[,\s]\d{3})+            # 750,000 / 1 200 000
       |\d+(?:\.\d+)?)                 # 750000 / 1.2
    \s?(k|K|m|M|thousand|million)?     # optional magnitude suffix
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BUDGET_CONTEXT_RE = re.compile(
    r"\b(budget|afford|spend|price\s*range|up\s*to|around|under|below|"
    r"looking\s*to\s*spend)\b"
    r"|[$€£¥₹]",  # any currency symbol is also a budget-context cue
    re.IGNORECASE,
)

# Time-constraint language — urgency / deadline signal.
# "this week / today / tomorrow / urgently / ASAP / by Friday / deadline"
_TIME_CONSTRAINT_RE = re.compile(
    r"\b("
    r"this\s+week|this\s+month|this\s+weekend|today|tomorrow|"
    r"urgently|urgent|asap|a\.s\.a\.p\.|right\s+away|immediately|"
    r"by\s+(?:end\s+of\s+)?(?:the\s+)?(?:week|month|day|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"deadline|time.sensitive|time\s+sensitive|need\s+it\s+(?:done\s+)?(?:now|soon)|"
    r"as\s+soon\s+as\s+possible|within\s+(?:the\s+)?\d+\s+(?:day|week|hour)s?|"
    r"before\s+(?:end\s+of\s+)?(?:the\s+)?(?:week|month|next|this)"
    r")\b",
    re.IGNORECASE,
)

# Purchase / booking / engagement intent — domain-agnostic.
# Covers buying, booking an appointment, hiring, engaging a service,
# getting a quote, placing an order, signing up, enrolling, scheduling.
_PURCHASE_INTENT_RE = re.compile(
    r"\b("
    r"buy|buying|purchase|purchased|"
    r"book|booking|booked|reserve|reservation|"
    r"hire|hiring|engage|engaging|"
    r"sign\s+(?:me\s+)?up|sign(?:ing)?\s+up|"
    r"place\s+(?:an\s+)?order|order(?:ing)?|"
    r"enroll|enrolling|"
    # schedule + any appointment/session noun (including consultation)
    r"schedule\s+(?:a\s+)?(?:call|consult(?:ation)?|appointment|meeting|"
    r"session|visit|demo|evaluation|assessment|interview)|"
    r"(?:schedule|book)\s+(?:an?\s+)?(?:appointment|consultation|interview|"
    r"evaluation|assessment|demo|session|meeting)|"
    r"get\s+(?:a\s+)?(?:quote|proposal|estimate|consult(?:ation)?)|"
    r"interested\s+in\s+(?:buying|purchasing|booking|hiring)|"
    r"(?:want|looking|would\s+like|like)\s+to\s+(?:buy|purchase|book|hire|"
    r"sign\s+up|order|"
    r"get\s+(?:a\s+)?(?:quote|proposal|estimate|consult(?:ation)?)|schedule)|"
    r"ready\s+to\s+(?:buy|purchase|book|start|proceed|move\s+forward)|"
    r"can\s+I\s+(?:book|schedule|order|hire|"
    r"get\s+(?:a\s+)?(?:quote|proposal|consult(?:ation)?))"
    r")\b",
    re.IGNORECASE,
)

# Magnitudes for normalising a money match to an absolute value.
_MAGNITUDE = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "thousand": 1_000.0,
    "million": 1_000_000.0,
}

# Scoring weights per §3.4.5.
WEIGHT_BUDGET: float = 0.5
WEIGHT_TIME_CONSTRAINT: float = 0.3
WEIGHT_PURCHASE_INTENT: float = 0.4
SCORE_CAP: float = 1.0

# Minimum score for the high-value-lead escalation gate to fire.
# The built-in threshold fires when ANY scored signal fires (raw >= 0.3).
# Pro/Enterprise can boost this further via business_context_rules.
_SCORE_FIRE_THRESHOLD: float = 0.3


@dataclass
class LeadCandidate:
    """A conversation that crossed the §3.4.4 lead threshold.

    Carries the structured fields the dashboard lead row needs. Fields
    are best-effort — a lead crosses on ANY one qualifying signal, so not
    every field is always populated.

    ``lead_value`` is the extracted budget (when a budget triggered /
    accompanied the threshold), surfaced so the §3.4.5 high-value-lead
    OUTCOME signal can read the SAME value the lead row recorded (one
    extraction, two consumers).

    ``lead_score`` is the weighted composite score in [0, 1] (§3.4.5).
    It is emitted as ``signal_confidence`` on the escalation event.
    """

    triggers: list[str] = field(default_factory=list)
    name: str | None = None
    contact_channel: str | None = None
    contact_identifier: str | None = None
    intent: str | None = None
    key_facts: list[str] = field(default_factory=list)
    next_step: str | None = None
    lead_value: float | None = None
    lead_score: float = 0.0


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

    The four trigger signals are:
      1. contact_info   — email or phone number detected
      2. budget         — currency/locale-tolerant money amount in context
      3. time_constraint — urgency language (this week, today, deadline…)
      4. purchase_intent — domain-agnostic buying/booking/hiring intent

    The ``lead_score`` field carries the weighted composite score [0, 1]:
    budget × 0.5 + time_constraint × 0.3 + purchase_intent × 0.4, capped
    at 1.0. This score is emitted as ``signal_confidence`` on the
    escalation event.

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

        # 3. Time-constraint signal — urgency / deadline language.
        time_m = _TIME_CONSTRAINT_RE.search(blob)
        if time_m:
            candidate.triggers.append("time_constraint")
            candidate.key_facts.append(f"urgency: {time_m.group(0).strip()}")

        # 4. Purchase / booking / engagement intent.
        intent_m = _PURCHASE_INTENT_RE.search(blob)
        if intent_m:
            candidate.triggers.append("purchase_intent")
            candidate.key_facts.append(f"intent: {intent_m.group(0).strip()}")
            if candidate.intent is None:
                candidate.intent = f"purchase/booking intent: {intent_m.group(0).strip()}"

        if not candidate.triggers:
            return None

        # Compute the weighted composite score over the three value-signals.
        # Contact-info is a threshold qualifier but does not add score weight
        # (it has no dollar value; scoring captures VALUE signals).
        raw_score = 0.0
        if "budget" in candidate.triggers:
            raw_score += WEIGHT_BUDGET
        if "time_constraint" in candidate.triggers:
            raw_score += WEIGHT_TIME_CONSTRAINT
        if "purchase_intent" in candidate.triggers:
            raw_score += WEIGHT_PURCHASE_INTENT
        candidate.lead_score = min(raw_score, SCORE_CAP)

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


def score_lead(
    candidate: LeadCandidate | None,
    *,
    business_context_rules: list[dict] | None = None,
) -> float:
    """Compute the final normalized lead score [0, 1].

    Applies optional Pro/Enterprise business-context custom rules on top
    of the built-in weighted composite score.

    ``business_context_rules`` is a list of dicts, each with:
      {"pattern": "<regex str>", "weight_boost": <float>}
    Each matching rule adds its ``weight_boost`` to the raw score before
    the 1.0 cap. Free tier passes ``None`` or ``[]`` — only built-in
    scoring applies.

    Returns 0.0 when ``candidate`` is ``None`` or has no scored signals.
    """
    if candidate is None:
        return 0.0

    raw = candidate.lead_score  # already computed in detect()

    if business_context_rules:
        # Build the blob from key_facts + intent for matching.
        context_blob = " ".join(
            [candidate.intent or ""] + list(candidate.key_facts)
        )
        for rule in business_context_rules:
            try:
                pattern = rule.get("pattern", "")
                boost = float(rule.get("weight_boost", 0.0))
                if pattern and re.search(pattern, context_blob, re.IGNORECASE):
                    raw += boost
            except Exception:  # noqa: BLE001
                pass

    return min(raw, SCORE_CAP)


def _extract_budget(blob: str) -> float | None:
    """Return the largest money amount mentioned in a budget context.

    Requires a budget-context cue (currency symbol, "budget", "spend", etc.)
    so a bare number ("I have 2 kids") does not register as a budget.

    Currency/locale-tolerant: $ € £ ¥ ₹ prefixes, k/m/thousand/million
    suffixes, comma/space thousands separators.

    Documented limits: written-out amounts ("five thousand dollars"),
    CNY/JPY numeric-only formats without a recognised symbol, and bare
    numbers below $100 are not detected (floor filters street numbers,
    item counts, and trivially small amounts).

    The floor was lowered from $1,000 to $100 in RESCAN TIER-C to
    accommodate domain-agnostic verticals (medical-spa bookings, legal
    consultations, etc.) where $200-$900 are legitimate lead budgets.
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
        # Ignore implausibly small amounts — likely a street/unit number,
        # an item count, or a trivially small fee. Floor = $100.
        # RESCAN TIER-C: lowered from $1,000 (real-estate bias) to $100
        # so domain-agnostic budgets (spa $300, legal $500) are detected.
        if value < 100:
            continue
        if best is None or value > best:
            best = value
    return best


def _default_next_step(candidate: LeadCandidate) -> str:
    if "purchase_intent" in candidate.triggers:
        return "follow up to complete purchase/booking"
    if "contact_info" in candidate.triggers:
        return "follow up with the customer"
    if "budget" in candidate.triggers:
        return "share matching options"
    if "time_constraint" in candidate.triggers:
        return "reach out promptly — time-sensitive"
    return "follow up with the customer"
