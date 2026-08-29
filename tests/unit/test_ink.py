from __future__ import annotations

import unittest
from unittest.mock import patch

from mystic import ink


class FoundationTests(unittest.TestCase):
    def test_color_helpers_return_plain_text_when_color_disabled(self) -> None:
        with patch("mystic.ink._supports_color", return_value=False):
            self.assertEqual(ink.green("ok"), "ok")
            self.assertEqual(ink.red("bad"), "bad")

    def test_color_helpers_wrap_ansi_when_color_enabled(self) -> None:
        with patch("mystic.ink._supports_color", return_value=True):
            colored = ink.green("ok")

        self.assertIn("\x1b[32m", colored)
        self.assertEqual(ink.strip_ansi(colored), "ok")

    def test_format_duration_covers_seconds_minutes_and_hours(self) -> None:
        self.assertEqual(ink.format_duration(59_000), "59s")
        self.assertEqual(ink.format_duration(61_000), "1m 1s")
        self.assertEqual(ink.format_duration(3_660_000), "1h 1m")

    def test_format_phone_formats_us_numbers_and_passthroughs_other_values(self) -> None:
        self.assertEqual(ink.format_phone("+14155551234"), "+1 (415) 555-1234")
        self.assertEqual(ink.format_phone("+442071838750"), "+442071838750")

    def test_status_icon_returns_labeled_icons(self) -> None:
        with patch("mystic.ink._supports_color", return_value=False):
            self.assertEqual(ink.status_icon("running"), "[up]")
            self.assertEqual(ink.status_icon("stopped"), "[down]")
            self.assertEqual(ink.status_icon("unknown"), "[?]")

    def test_box_renders_unicode_border(self) -> None:
        with patch("mystic.ink._supports_unicode", return_value=True):
            rendered = ink.box(["Alpha", "Beta"])

        self.assertIn("╭", rendered)
        self.assertIn("Alpha", rendered)
        self.assertIn("╰", rendered)

    def test_box_falls_back_to_ascii(self) -> None:
        with patch("mystic.ink._supports_unicode", return_value=False):
            rendered = ink.box(["One"], width=4)

        self.assertIn("+", rendered)
        self.assertIn("One", rendered)


if __name__ == "__main__":
    unittest.main()
