from __future__ import annotations

import unittest

from mystic.config import (
    format_identity,
    identity_exists,
    parse_identity,
    read_identity,
    read_identity_raw,
    write_identity,
)
from mystic.types import Identity
from tests.python_helpers import TempAppHome


class IdentityTests(unittest.TestCase):
    def test_parse_identity_defaults_missing_fields(self) -> None:
        parsed = parse_identity("# Identity\n")
        self.assertEqual(parsed, Identity(name="", creature="", vibe="", emoji=""))

    def test_format_identity_matches_expected_layout(self) -> None:
        identity = Identity(name="Lyra", creature="fox spirit", vibe="sharp and warm", emoji=":sparkles:")
        formatted = format_identity(identity)
        self.assertIn("- **Name:** Lyra", formatted)
        self.assertIn("- **Creature:** fox spirit", formatted)
        self.assertTrue(formatted.endswith("\n"))

    def test_write_and_read_identity_round_trip(self) -> None:
        with TempAppHome():
            identity = Identity(name="Lyra", creature="fox spirit", vibe="sharp and warm", emoji=":sparkles:")
            self.assertFalse(identity_exists())
            write_identity(identity)
            self.assertTrue(identity_exists())
            self.assertEqual(read_identity(), identity)
            self.assertIn("Lyra", read_identity_raw())

    def test_read_identity_raises_when_missing(self) -> None:
        with TempAppHome():
            with self.assertRaises(FileNotFoundError):
                read_identity()
