from __future__ import annotations

import unittest

from mystic.config import get_error_message, logger, silence_stdout, get_home
from tests.python_helpers import TempAppHome


class LoggerTests(unittest.TestCase):
    def test_get_error_message_returns_exception_text(self) -> None:
        self.assertEqual(get_error_message(RuntimeError("boom")), "boom")
        self.assertEqual(get_error_message("plain"), "plain")

    def test_logger_writes_json_log_file(self) -> None:
        with TempAppHome() as home:
            silence_stdout(True)
            logger.info("test.event", answer=42)
            log_path = get_home() / "logs" / "mystic-horizon.log"
            self.assertTrue(log_path.exists())
            self.assertIn("\"event\": \"test.event\"", log_path.read_text(encoding="utf-8"))
            self.assertIn(str(home / "logs"), str(log_path.parent))
