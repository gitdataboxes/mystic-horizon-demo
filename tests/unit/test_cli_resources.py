from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from mystic.cli import cli


# ── tiny fakes ──

@dataclass
class FakePerson:
    id: str = "p1"
    name: str = "Alice"
    phone: str = "+15551234567"

@dataclass
class FakeCall:
    id: str = "c1"
    person_id: str = "p1"
    direction: str = "inbound"
    status: str = "completed"

@dataclass
class FakeAction:
    id: str = "a1"
    person_id: str = "p1"
    intent: str = "Follow up"
    status: str = "pending"
    urgency: str = "normal"

@dataclass
class FakeFact:
    id: str = "f1"
    person_id: str = "p1"
    type: str = "identity"
    content: str = "Likes coffee"
    confidence: float = 1.0

@dataclass
class FakeFaqChunk:
    id: str = "faq1"
    heading: str = "Billing"
    content: str = "We accept all major cards."


def _fake_db():
    return MagicMock()


def _agent_env():
    return patch("mystic.cli._apply_agent_env")


class CliSurfaceTests(unittest.TestCase):
    """Existing tests."""

    def test_setup_command_emits_json(self) -> None:
        runner = CliRunner()
        with patch(
            "mystic.cli.run_setup",
            new=AsyncMock(return_value={"status": "ready", "agent": "mystic-1", "dashboard": "http://localhost:3000/dashboard"}),
        ):
            result = runner.invoke(cli, ["setup"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["agent"], "mystic-1")

    def test_chat_command_emits_single_json_object(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.run_chat_json", new=AsyncMock(return_value=[{"response": "hello"}])),
        ):
            result = runner.invoke(cli, ["--agent", "sales", "chat", "--message", "hi"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["response"], "hello")

    def test_health_command_returns_exit_code_from_health_runner(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.run_health", return_value=({"status": "degraded"}, 1)),
        ):
            result = runner.invoke(cli, ["--agent", "sales", "health"])

        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "degraded")

    def test_dashboard_files_command_emits_json(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.list_dashboard_files", return_value=["style.css", "pages/home.html"]),
        ):
            result = runner.invoke(cli, ["--agent", "sales", "dashboard", "files"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["files"], ["style.css", "pages/home.html"])


class StartStopTests(unittest.TestCase):
    def test_start_emits_json(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.run_start", return_value={"status": "started", "pid": 123, "port": 8080}),
        ):
            result = runner.invoke(cli, ["--agent", "test", "start"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("status", payload)
        self.assertEqual(payload["pid"], 123)

    def test_stop_emits_json(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.run_stop", return_value={"status": "stopped", "pid": 123}),
        ):
            result = runner.invoke(cli, ["--agent", "test", "stop"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "stopped")


class PeopleTests(unittest.TestCase):
    def test_people_list(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_all_people", return_value=[FakePerson()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "people", "list"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("people", payload)
        self.assertEqual(len(payload["people"]), 1)

    def test_people_get(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_person_by_id", return_value=FakePerson()),
            patch("mystic.cli.get_all_active_facts_by_person", return_value=[]),
            patch("mystic.cli.get_recent_calls_by_person", return_value=[]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "people", "get", "p1"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("person", payload)
        self.assertIn("facts", payload)
        self.assertIn("calls", payload)


class CallsTests(unittest.TestCase):
    def test_calls_list(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_recent_calls", return_value=[FakeCall(), FakeCall()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "calls", "list"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("calls", payload)
        self.assertEqual(len(payload["calls"]), 2)

    def test_calls_active(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.list_active_calls", return_value=[FakeCall()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "calls", "active"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("calls", payload)
        self.assertEqual(len(payload["calls"]), 1)


class ActionsTests(unittest.TestCase):
    def test_actions_list(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_all_pending_actions", return_value=[FakeAction()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "actions", "list"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("actions", payload)
        self.assertEqual(len(payload["actions"]), 1)

    def test_actions_get(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_action_by_id", return_value=FakeAction()),
        ):
            result = runner.invoke(cli, ["--agent", "test", "actions", "get", "a1"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("action", payload)
        self.assertEqual(payload["action"]["id"], "a1")

    def test_actions_create(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.insert_action", return_value=FakeAction(id="a2", intent="Call back")),
        ):
            result = runner.invoke(cli, [
                "--agent", "test", "actions", "create",
                "--person", "p1", "--intent", "Call back",
            ])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("action", payload)
        self.assertEqual(payload["action"]["intent"], "Call back")


class FactsTests(unittest.TestCase):
    def test_facts_list(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_all_active_facts_by_person", return_value=[FakeFact()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "facts", "list", "--person", "p1"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("facts", payload)
        self.assertEqual(len(payload["facts"]), 1)

    def test_facts_search(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.search_facts", return_value=[FakeFact()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "facts", "search", "coffee"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("facts", payload)


class IdentitySoulTests(unittest.TestCase):
    def test_identity_show(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.read_identity_raw", return_value="I am Mystic."),
            patch("mystic.cli.get_identity_path", return_value=Path("/tmp/IDENTITY.md")),
        ):
            result = runner.invoke(cli, ["--agent", "test", "identity", "show"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("content", payload)
        self.assertIn("path", payload)
        self.assertEqual(payload["content"], "I am Mystic.")

    def test_soul_show(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.read_soul", return_value="Be helpful."),
            patch("mystic.cli.get_soul_path", return_value=Path("/tmp/SOUL.md")),
        ):
            result = runner.invoke(cli, ["--agent", "test", "soul", "show"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("content", payload)
        self.assertIn("path", payload)
        self.assertEqual(payload["content"], "Be helpful.")


class ConfigTests(unittest.TestCase):
    def test_config_show(self) -> None:
        fake_home = Path("/tmp/fake-agent")
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.get_home", return_value=fake_home),
            patch("mystic.cli._read_json_file", return_value={"server": {"port": 8080}}),
        ):
            result = runner.invoke(cli, ["--agent", "test", "config", "show"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("agent", payload)
        self.assertIn("providers", payload)
        self.assertIn("intelligence", payload)


class DialSmsEmailTests(unittest.TestCase):
    def test_dial_emits_json(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.run_dial", new=AsyncMock(return_value={
                "status": "dialing", "call_id": "c1", "action_id": "a1",
                "phone": "+15551234567", "intent": "Check in",
            })),
        ):
            result = runner.invoke(cli, ["--agent", "test", "dial", "+15551234567", "Check", "in"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "dialing")
        self.assertIn("call_id", payload)

    def test_sms_emits_json(self) -> None:
        runner = CliRunner()
        fake_twilio = MagicMock()
        with (
            _agent_env(),
            patch("mystic.cli.get_providers_config", return_value=MagicMock(twilio=fake_twilio)),
            patch("mystic.cli.send_sms", new=AsyncMock(return_value="SM123")),
        ):
            result = runner.invoke(cli, ["--agent", "test", "sms", "+15551234567", "--body", "Hello!"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "sent")
        self.assertIn("sid", payload)
        self.assertEqual(payload["phone"], "+15551234567")

    def test_email_emits_json(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.send_email", new=AsyncMock(return_value=None)),
        ):
            result = runner.invoke(cli, [
                "--agent", "test", "email", "bob@example.com",
                "--subject", "Hi", "--body", "Test body",
            ])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["to"], "bob@example.com")
        self.assertEqual(payload["subject"], "Hi")


class FaqTests(unittest.TestCase):
    def test_faq_list(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli.open_database", return_value=_fake_db()),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.get_all_faq_chunks", return_value=[FakeFaqChunk()]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "faq", "list"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("faq", payload)
        self.assertEqual(len(payload["faq"]), 1)

    def test_faq_search(self) -> None:
        runner = CliRunner()
        with (
            _agent_env(),
            patch("mystic.cli._search_faq_locally", return_value=[{"heading": "Billing", "content": "We accept all major cards."}]),
        ):
            result = runner.invoke(cli, ["--agent", "test", "faq", "search", "billing"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("faq", payload)
