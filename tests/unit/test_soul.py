from __future__ import annotations

import unittest
from unittest.mock import patch

from mystic.config import list_journal_entries, read_soul, soul_exists, write_soul
from tests.python_helpers import TempAppHome


class SoulTests(unittest.TestCase):
    def test_write_and_read_soul(self) -> None:
        with TempAppHome():
            self.assertFalse(soul_exists())
            write_soul("First soul")
            self.assertTrue(soul_exists())
            self.assertEqual(read_soul(), "First soul")

    def test_write_soul_creates_journal_entry_when_replacing_existing_file(self) -> None:
        with TempAppHome():
            with patch("mystic.config.time.time", return_value=2.0):
                write_soul("First soul")
            with patch("mystic.config.time.time", return_value=3.0):
                write_soul("Second soul")
            self.assertEqual(read_soul(), "Second soul")
            entries = list_journal_entries("soul")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].timestamp, 3000)
            self.assertEqual(entries[0].content, "First soul")

    def test_read_soul_raises_when_missing(self) -> None:
        with TempAppHome():
            with self.assertRaises(FileNotFoundError):
                read_soul()
