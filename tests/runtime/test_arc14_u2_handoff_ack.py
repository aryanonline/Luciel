"""Arc 14 U2 — Gate-1 handoff acknowledgement template tests."""
from __future__ import annotations

import unittest

from app.runtime.handoff_ack import (
    DEFAULT_PRESET,
    PRESET_FRIENDLY_EXPERT,
    PRESET_PROFESSIONAL_ADVISOR,
    PRESET_TRUSTED_AUTHORITY,
    PRESET_WARM_CONCIERGE,
    handoff_acknowledgement,
)


class TestHandoffAck(unittest.TestCase):

    def test_default_preset_returns_nonempty(self):
        self.assertTrue(handoff_acknowledgement())

    def test_unknown_preset_falls_back_to_default(self):
        self.assertEqual(
            handoff_acknowledgement(preset="does_not_exist"),
            handoff_acknowledgement(preset=DEFAULT_PRESET),
        )

    def test_none_preset_falls_back_to_default(self):
        self.assertEqual(
            handoff_acknowledgement(preset=None),
            handoff_acknowledgement(preset=DEFAULT_PRESET),
        )

    def test_all_named_presets_have_distinct_nonempty_copy(self):
        presets = [
            PRESET_WARM_CONCIERGE,
            PRESET_PROFESSIONAL_ADVISOR,
            PRESET_FRIENDLY_EXPERT,
            PRESET_TRUSTED_AUTHORITY,
        ]
        copies = {handoff_acknowledgement(preset=p) for p in presets}
        self.assertEqual(len(copies), len(presets))
        for c in copies:
            self.assertTrue(c.strip())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
