from __future__ import annotations

import os
import unittest
from pathlib import Path

from mystic.config import get_home, get_shared_home, is_valid_agent_name, resolve_agent_home, validate_agent_name


class PathsTests(unittest.TestCase):
    def test_get_home_prefers_app_home(self) -> None:
        old = os.environ.get("APP_HOME")
        try:
            os.environ["APP_HOME"] = "/tmp/mystic-phase1"
            self.assertEqual(get_home(), Path("/tmp/mystic-phase1"))
        finally:
            if old is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = old

    def test_get_home_defaults_to_shared_home(self) -> None:
        old = os.environ.get("APP_HOME")
        try:
            os.environ.pop("APP_HOME", None)
            self.assertEqual(get_home(), get_shared_home())
        finally:
            if old is not None:
                os.environ["APP_HOME"] = old

    def test_agent_name_validation_matches_cli_rules(self) -> None:
        self.assertTrue(is_valid_agent_name("sales"))
        self.assertTrue(is_valid_agent_name("my-agent"))
        self.assertFalse(is_valid_agent_name("UPPERCASE"))
        self.assertFalse(is_valid_agent_name("../escape"))
        self.assertEqual(validate_agent_name("agent1"), "agent1")
        with self.assertRaisesRegex(ValueError, "Invalid agent name"):
            validate_agent_name("bad_name")

    def test_resolve_agent_home_uses_shared_root(self) -> None:
        self.assertEqual(resolve_agent_home("support"), get_shared_home() / "support")
