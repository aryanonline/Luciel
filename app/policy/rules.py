"""
Policy rules configuration.

Defines the specific rules and thresholds the policy engine uses.
Keeping rules separate from the engine makes them easier to
adjust per tenant or domain later.

For MVP, these are simple constants.
Later, these can be loaded from DB or tenant config.
"""

# Maximum response length before truncation
MAX_RESPONSE_LENGTH = 10000

# Minimum memory content length to save
MIN_MEMORY_LENGTH = 5

# Valid memory categories
VALID_MEMORY_CATEGORIES = {
    "preference",
    "constraint",
    "goal",
    "fact",
    "operational",
}

# Default escalation message when the reply is empty after cleanup
DEFAULT_ESCALATION_MESSAGE = (
    "I understand you'd like to speak with someone directly. "
    "I'm flagging this conversation for a team member to follow up with you. "
    "They'll be in touch shortly."
)