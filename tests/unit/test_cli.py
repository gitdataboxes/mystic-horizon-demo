from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

from click.testing import CliRunner

from mystic.cli import (
    RuntimeStateRecord,
    InitSelections,
    UsedPorts,
    _default_setup_selections,
    _show_init_summary,
    _prompt_init_selections,
    _prompt_quick_init,
    allocate_port,
    cleanup_runtime_files,
    discover_siblings,
    discover_used_ports,
    ensure_dependencies,
    extract_sibling_keys,
    list_agent_dirs,
    parse_agent_flag,
    prepare_cli_args,
    read_pid,
    read_runtime_state,
    run_create_migration,
    run_connect_calendar,
    run_connect_hub_calendar,
    run_connect_smtp,
    run_health,
    run_setup,
    run_status,
    run_status_all,
    seed_prompt_files,
    write_config_files,
    write_pid,
    write_runtime_state,
    cli,
)
from mystic.calls import DEFAULT_VOICE_ID
from mystic.config import clear_config_cache, get_agent_config, get_intelligence_config, get_providers_config
from mystic.db import close_database, initialize_schema, open_database
from mystic.http import HttpResponse
from mystic.voice import ensure_pocket_onnx_models
from tests.python_helpers import TempAppHome, seed_core_files


class CliArgParsingTests(unittest.TestCase):
    def test_parse_agent_flag_extracts_agent_after_command(self) -> None:
        agent_name, remaining = parse_agent_flag(["status", "--agent", "sales", "--detail"])
        self.assertEqual(agent_name, "sales")
        self.assertEqual(remaining, ["status", "--detail"])

    def test_prepare_cli_args_uses_environment_agent(self) -> None:
        old = os.environ.get("MH_AGENT")
        try:
            os.environ["MH_AGENT"] = "support"
            prepared = prepare_cli_args(["status"])
        finally:
            if old is None:
                os.environ.pop("MH_AGENT", None)
            else:
                os.environ["MH_AGENT"] = old
        self.assertEqual(prepared.agent_name, "support")
        self.assertEqual(prepared.click_args, ["status"])

    def test_click_cli_rejects_invalid_agent_name(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--all", "--agent", "bad_name"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid agent name", result.output)

    def test_click_cli_dispatches_create_migration_command(self) -> None:
        runner = CliRunner()
        with (
            patch("mystic.cli._apply_agent_env"),
            patch("mystic.cli.run_create_migration") as run_create_migration_mock,
        ):
            result = runner.invoke(cli, ["--agent", "sales", "create_migration", "add", "events"])

        self.assertEqual(result.exit_code, 0)
        run_create_migration_mock.assert_called_once_with("add events")


class MultiAgentHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="mh-cli-tests-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_list_agent_dirs_skips_bin_and_non_dirs(self) -> None:
        (self.root / "alpha").mkdir()
        (self.root / "beta").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "notes.txt").write_text("ignore", encoding="utf-8")

        self.assertEqual(list_agent_dirs(self.root), ["alpha", "beta"])

    def test_discover_siblings_excludes_current_agent(self) -> None:
        self._write_providers("alpha", {"twilio": {"accountSid": "AC1"}})
        self._write_providers("beta", {"twilio": {"accountSid": "AC2"}})

        self.assertEqual(discover_siblings("alpha", self.root), ["beta"])

    def test_extract_sibling_keys_skips_per_agent_values(self) -> None:
        self._write_providers(
            "existing",
            {
                "twilio": {
                    "accountSid": "AC1",
                    "authToken": "tok1",
                    "phoneNumber": "+15551111111",
                    "phoneNumberSid": "PN1",
                },
                "stt": {"provider": "moonshine", "model": "small"},
                "tts": {"provider": "inworld", "apiKey": "iw1"},
                "embedding": {"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
                "openrouter": {"apiKey": "or1"},
            },
        )

        keys = extract_sibling_keys("existing", self.root)
        self.assertEqual(keys["twilioSid"], "AC1")
        self.assertEqual(keys["twilioToken"], "tok1")
        self.assertEqual(keys["openrouter"], "or1")
        self.assertEqual(keys["inworld"], "iw1")
        self.assertNotIn("phoneNumber", keys)
        self.assertNotIn("phoneNumberSid", keys)

    def test_extract_sibling_keys_includes_deepgram_api_key(self) -> None:
        self._write_providers(
            "existing",
            {
                "stt": {"provider": "deepgram", "apiKey": "dg1"},
                "tts": {"provider": "pocket"},
            },
        )

        keys = extract_sibling_keys("existing", self.root)
        self.assertEqual(keys["deepgram"], "dg1")

    def test_extract_sibling_keys_reads_twilio_draft_credentials(self) -> None:
        self._write_providers(
            "existing",
            {
                "twilioDraft": {
                    "accountSid": "AC-draft",
                    "authToken": "tok-draft",
                },
            },
        )

        keys = extract_sibling_keys("existing", self.root)
        self.assertEqual(keys["twilioSid"], "AC-draft")
        self.assertEqual(keys["twilioToken"], "tok-draft")

    def test_discover_used_ports_and_allocate_port(self) -> None:
        self._write_agent_config("alpha", server_port=3000, livekit_port=7880)
        self._write_agent_config("beta", server_port=3001, livekit_port=7881)

        used = discover_used_ports("alpha", self.root)
        self.assertEqual(used.server_ports, {3001})
        # beta's livekit port 7881 expands to {7881, 7882, 7883}
        self.assertEqual(used.livekit_ports, {7881, 7882, 7883})
        with patch("mystic.cli._port_in_use", return_value=False):
            self.assertEqual(allocate_port(3000, used.server_ports), 3000)
            # stride=3 skips past beta's full range (7881-7883)
            self.assertEqual(allocate_port(7880, used.livekit_ports, 7880, stride=3), 7884)

    def _write_providers(self, name: str, providers: dict[str, object]) -> None:
        config_dir = self.root / name / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "providers.json").write_text(json.dumps(providers), encoding="utf-8")

    def _write_agent_config(self, name: str, *, server_port: int, livekit_port: int) -> None:
        config_dir = self.root / name / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.json").write_text(
            json.dumps({"server": {"port": server_port}}),
            encoding="utf-8",
        )
        (config_dir / "providers.json").write_text(
            json.dumps({"livekit": {"port": livekit_port}}),
            encoding="utf-8",
        )


class InitHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()

    def tearDown(self) -> None:
        clear_config_cache()
        cleanup_runtime_files()
        self.temp_home.__exit__(None, None, None)

    def test_seed_prompt_files_creates_prompt_tree_faq_and_migration(self) -> None:
        seed_prompt_files(self.home)

        self.assertTrue((self.home / "prompts" / "shared" / "context.md").exists())
        self.assertTrue((self.home / "prompts" / "owner" / "briefing.md").exists())
        self.assertTrue((self.home / "prompts" / "public" / "workflow.md").exists())
        self.assertTrue((self.home / "faq" / "about-me.md").exists())
        self.assertTrue((self.home / "migrations" / "001.sql").exists())
        self.assertTrue((self.home / "migrations" / "002.sql").exists())
        self.assertTrue((self.home / "migrations" / "003.sql").exists())
        self.assertTrue((self.home / "migrations" / "004.sql").exists())
        self.assertTrue((self.home / "migrations" / "005.sql").exists())
        self.assertTrue((self.home / "migrations" / "006.sql").exists())

    def test_run_create_migration_creates_next_numbered_file(self) -> None:
        seed_prompt_files(self.home)

        file_path = run_create_migration("add sample table")

        self.assertEqual(file_path, self.home / "migrations" / "010_add_sample_table.sql")
        self.assertTrue(file_path.exists())
        self.assertIn("runner wraps this file in a transaction", file_path.read_text(encoding="utf-8"))

    def test_write_config_files_produces_loadable_configs(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "small"},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
            llm_backend={"provider": "custom", "baseURL": "http://llm.local/v1", "model": "local-model"},
            openrouter_key="openrouter-key",
        )

        write_config_files(self.home, selections)

        agent = get_agent_config()
        providers = get_providers_config()
        intelligence = get_intelligence_config()
        self.assertIsNone(agent.owner.phone)
        self.assertEqual(agent.server.port, 3456)
        self.assertEqual(agent.agent.voiceId, "Hades")
        self.assertEqual(providers.livekit.port, 7880)
        self.assertEqual(providers.embedding.provider, "local")
        self.assertEqual(getattr(providers.tts, "provider", ""), "pocket")
        self.assertEqual(intelligence.search.model, "perplexity/sonar-pro")

    def test_prompt_init_selections_defaults_to_hades_and_skips_python_command(self) -> None:
        with (
            patch("mystic.cli.discover_used_ports", return_value=UsedPorts(set(), set())),
            patch("mystic.cli._detect_timezone", return_value="America/Los_Angeles"),
            patch(
                "mystic.cli._prompt_llm_config",
                return_value=(
                    {"provider": "openrouter", "model": "rt-model"},
                    {"provider": "openrouter", "model": "backend-model"},
                    "openrouter-key",
                ),
            ),
            patch(
                "mystic.cli.click.prompt",
                side_effect=["", "America/Los_Angeles", "pocket", "", "moonshine", "small"],
            ) as prompt_mock,
        ):
            selections = _prompt_init_selections("alpha", {})

        self.assertEqual(selections.selected_voice_id, DEFAULT_VOICE_ID)
        self.assertEqual(selections.tts_config, {"provider": "pocket"})
        self.assertEqual(prompt_mock.call_count, 6)

    def test_default_setup_selections_use_cloud_setup_defaults(self) -> None:
        with (
            patch("mystic.cli.discover_used_ports", return_value=UsedPorts(set(), set())),
            patch("mystic.cli._detect_timezone", return_value="America/Los_Angeles"),
        ):
            selections = _default_setup_selections("alpha")

        self.assertEqual(selections.selected_voice_id, "Olivia")
        self.assertEqual(selections.llm_realtime, {"provider": "openrouter", "model": "openai/gpt-5.5"})
        self.assertEqual(selections.llm_backend, {"provider": "openrouter", "model": "openai/gpt-5.5"})

    def test_prompt_quick_init_supports_inworld_tts(self) -> None:
        with (
            patch("mystic.cli.discover_used_ports", return_value=UsedPorts(set(), set())),
            patch("mystic.cli._detect_timezone", return_value="America/Los_Angeles"),
            patch("mystic.cli._pick", side_effect=["inworld", "Clive"]),
            patch("mystic.cli._prompt_secret", side_effect=["openrouter-key", "inworld-key"]),
        ):
            selections = _prompt_quick_init(
                "alpha",
                {"openrouter": "openrouter-key", "inworld": "inworld-key"},
            )

        self.assertEqual(selections.selected_voice_id, "Clive")
        self.assertEqual(
            selections.tts_config,
            {"provider": "inworld", "apiKey": "inworld-key"},
        )

    def test_prompt_init_selections_supports_deepgram_stt(self) -> None:
        with (
            patch("mystic.cli.discover_used_ports", return_value=UsedPorts(set(), set())),
            patch("mystic.cli._detect_timezone", return_value="America/Los_Angeles"),
            patch(
                "mystic.cli._prompt_llm_config",
                return_value=(
                    {"provider": "openrouter", "model": "rt-model"},
                    {"provider": "openrouter", "model": "backend-model"},
                    "openrouter-key",
                ),
            ),
            patch(
                "mystic.cli.click.prompt",
                side_effect=["", "America/Los_Angeles", "pocket", "Hades", "deepgram"],
            ),
            patch("mystic.cli._prompt_secret", return_value="deepgram-key"),
        ):
            selections = _prompt_init_selections("alpha", {"deepgram": "deepgram-key"})

        self.assertEqual(selections.stt_config, {"provider": "deepgram", "apiKey": "deepgram-key"})

    def test_runtime_state_round_trips(self) -> None:
        write_pid(12345)
        write_runtime_state(
            RuntimeStateRecord(
                pid=12345,
                port=3456,
                tunnel_url="https://example.tail1234.ts.net",
                mode="full",
                started_at=1_700_000_000_000,
            )
        )

        self.assertEqual(read_pid(), 12345)
        state = read_runtime_state()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.port, 3456)
        self.assertEqual(state.tunnel_url, "https://example.tail1234.ts.net")


class ConnectCalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)

        providers_path = self.home / "config" / "providers.json"
        providers = json.loads(providers_path.read_text(encoding="utf-8"))
        providers["calendar"] = {
            "subscriptions": [{"url": "https://example.test/existing.ics", "label": "Existing"}],
            "syncIntervalMinutes": 20,
            "reminderMinutes": 5,
        }
        providers_path.write_text(json.dumps(providers, indent=2) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    async def test_run_connect_calendar_merges_config_and_syncs_new_subscription(self) -> None:
        with (
            patch(
                "mystic.cli.fetch_with_timeout",
                new=AsyncMock(return_value=HttpResponse(status_code=200, content=b"BEGIN:VCALENDAR")),
            ),
            patch("mystic.cli.click.prompt", side_effect=[
                "https://example.test/work.ics",
                "Work",
                20,
                5,
            ]),
            patch("mystic.cli.click.confirm", return_value=False),
            patch("mystic.calendar.sync_subscription", new=AsyncMock(return_value=3)) as sync_subscription,
        ):
            result = await run_connect_calendar(show_intro=False)

        self.assertTrue(result)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload["calendar"],
            {
                "subscriptions": [
                    {"url": "https://example.test/existing.ics", "label": "Existing"},
                    {"url": "https://example.test/work.ics", "label": "Work"},
                ],
                "syncIntervalMinutes": 20,
                "reminderMinutes": 5,
            },
        )
        sync_subscription.assert_awaited_once()


class ConnectSmtpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    async def test_run_connect_smtp_writes_provider_config(self) -> None:
        with (
            patch(
                "mystic.cli.click.prompt",
                side_effect=["smtp.example.com", 587, "mailer", "agent@example.com"],
            ),
            patch("mystic.cli._prompt_secret", return_value="secret"),
            patch("mystic.cli.click.confirm", return_value=True),
        ):
            result = await run_connect_smtp(show_intro=False)

        self.assertTrue(result)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload["smtp"],
            {
                "host": "smtp.example.com",
                "port": 587,
                "username": "mailer",
                "password": "secret",
                "fromAddress": "agent@example.com",
                "useTls": True,
            },
        )


class ConnectHubCalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    async def test_run_connect_hub_calendar_google_writes_config_and_tokens(self) -> None:
        with (
            patch("mystic.cli.click.prompt", side_effect=["google", "client-id", "primary"]),
            patch(
                "mystic.cli._prompt_secret",
                return_value="client-secret",
            ),
            patch(
                "mystic.calendar.run_oauth_flow",
                new=AsyncMock(return_value=type("Tokens", (), {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_at": 1_800_000_000,
                    "token_type": "Bearer",
                })()),
            ),
        ):
            result = await run_connect_hub_calendar(show_intro=False)

        self.assertTrue(result)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["calendar"]["hub"]["provider"], "google")
        self.assertEqual(payload["calendar"]["hub"]["calendarId"], "primary")
        token_payload = json.loads((self.home / "config" / "hub_tokens.json").read_text(encoding="utf-8"))
        self.assertEqual(token_payload["access_token"], "access-1")
        self.assertEqual(token_payload["refresh_token"], "refresh-1")

    async def test_run_connect_hub_calendar_caldav_validates_and_writes_config(self) -> None:
        with (
            patch("mystic.cli.click.prompt", side_effect=["caldav", "https://nextcloud.example.test", "/dav/cal/default/", "alice"]),
            patch("mystic.cli._prompt_secret", return_value="app-password"),
            patch(
                "mystic.cli.fetch_with_timeout",
                new=AsyncMock(return_value=HttpResponse(status_code=207, content=b"<multistatus />")),
            ) as fetch_mock,
        ):
            result = await run_connect_hub_calendar(show_intro=False)

        self.assertTrue(result)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["calendar"]["hub"]["provider"], "caldav")
        self.assertEqual(payload["calendar"]["hub"]["baseUrl"], "https://nextcloud.example.test")
        self.assertEqual(payload["calendar"]["hub"]["calendarId"], "/dav/cal/default/")
        fetch_mock.assert_awaited_once()


class StatusAllTests(unittest.TestCase):
    def test_run_status_all_lists_only_initialized_agents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-status-all-") as tempdir:
            root = Path(tempdir)
            self._write_agent(root, "alpha", port=3001, phone="+15550001111")
            self._write_agent(root, "beta", port=3002, phone="+15550002222")
            (root / "orphan").mkdir()
            (root / "bin").mkdir()

            payload = run_status_all(root=root)
            agents = cast(list[dict[str, object]], payload["agents"])
            names = [a["name"] for a in agents]
            self.assertIn("alpha", names)
            self.assertIn("beta", names)
            self.assertNotIn("orphan", names)

    def _write_agent(self, root: Path, name: str, *, port: int, phone: str) -> None:
        config_dir = root / name / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.json").write_text(json.dumps({"server": {"port": port}}), encoding="utf-8")
        (config_dir / "providers.json").write_text(
            json.dumps({"twilio": {"phoneNumber": phone}}),
            encoding="utf-8",
        )


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()

    def tearDown(self) -> None:
        clear_config_cache()
        cleanup_runtime_files()
        self.temp_home.__exit__(None, None, None)

    def test_run_status_detail_includes_schema_version(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "small"},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
            llm_backend={"provider": "custom", "baseURL": "http://llm.local/v1", "model": "local-model"},
            openrouter_key="openrouter-key",
            owner_phone="+15551234567",
        )
        write_config_files(self.home, selections)
        db = open_database()
        try:
            initialize_schema(db)
        finally:
            close_database(db)

        payload = run_status(detail=True)
        self.assertIn("database", payload)
        db_info = cast(dict[str, object], payload["database"])
        self.assertEqual(db_info["schema_version"], 9)
        self.assertEqual(db_info["migrations"], 9)


class DependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_dependencies_installs_and_downloads_turn_detector_during_init(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "small"},
            embedding_config={"provider": "mock"},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()) as ensure_python_extra_mock,
            patch("mystic.cli.asyncio.to_thread", new=AsyncMock()) as to_thread_mock,
        ):
            await ensure_dependencies(selections)

        ensure_python_extra_mock.assert_has_awaits([
            call(
                "livekit.plugins.turn_detector",
                "livekit-plugins-turn-detector",
                label="LiveKit turn detector",
            ),
            call(
                "moonshine_voice",
                "moonshine-voice",
                label="Moonshine Voice",
            ),
        ])
        self.assertEqual(to_thread_mock.await_count, 3)
        self.assertEqual(
            to_thread_mock.await_args_list[0].args[0].__name__,
            "_ensure_turn_detector_model_download",
        )
        self.assertEqual(
            to_thread_mock.await_args_list[1].args[0].__name__,
            "_ensure_moonshine_model_download",
        )
        self.assertEqual(to_thread_mock.await_args_list[1].args[1], "small")
        self.assertEqual(to_thread_mock.await_args_list[2].args[0], ensure_pocket_onnx_models)

    async def test_ensure_dependencies_downloads_moonshine_voice_model_during_init(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": ""},
            stt_config={"provider": "moonshine", "model": "small"},
            embedding_config={"provider": "mock"},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()) as ensure_python_extra_mock,
            patch("mystic.cli.asyncio.to_thread", new=AsyncMock()) as to_thread_mock,
        ):
            await ensure_dependencies(selections)

        ensure_python_extra_mock.assert_awaited_once_with(
            "moonshine_voice",
            "moonshine-voice",
            label="Moonshine Voice",
        )
        to_thread_mock.assert_awaited_once_with(ANY, "small")
        assert to_thread_mock.await_args is not None
        self.assertEqual(to_thread_mock.await_args.args[0].__name__, "_ensure_moonshine_model_download")

    async def test_ensure_dependencies_downloads_pocket_onnx_models_during_init(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": ""},
            embedding_config={"provider": "mock"},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()),
            patch("mystic.cli.asyncio.to_thread", new=AsyncMock()) as to_thread_mock,
        ):
            await ensure_dependencies(selections)

        to_thread_mock.assert_awaited_once()
        assert to_thread_mock.await_args is not None
        args = to_thread_mock.await_args.args
        self.assertEqual(args[0], ensure_pocket_onnx_models)
        self.assertTrue(callable(args[1]))

    async def test_ensure_dependencies_installs_inworld_plugin_during_init(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "inworld", "apiKey": "iw-key"},
            stt_config={"provider": ""},
            embedding_config={"provider": "mock"},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()) as ensure_python_extra_mock,
        ):
            await ensure_dependencies(selections)

        ensure_python_extra_mock.assert_awaited_once_with(
            "livekit.plugins.inworld",
            "livekit-plugins-inworld",
            label="LiveKit Inworld plugin",
        )

    async def test_ensure_dependencies_installs_deepgram_plugin_during_init(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": ""},
            stt_config={"provider": "deepgram", "apiKey": "dg-key"},
            embedding_config={"provider": "mock"},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()) as ensure_python_extra_mock,
        ):
            await ensure_dependencies(selections)

        ensure_python_extra_mock.assert_awaited_once_with(
            "livekit.plugins.deepgram",
            "livekit-plugins-deepgram",
            label="LiveKit Deepgram plugin",
        )

    async def test_ensure_dependencies_reports_download_steps(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "medium"},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )
        steps: list[str] = []

        async def _on_step(label: str) -> None:
            steps.append(label)

        async def _fake_to_thread(fn: object, *args: object) -> None:
            assert callable(fn)
            fn(*args)

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()),
            patch("mystic.cli._ensure_turn_detector_model_download"),
            patch("mystic.cli._ensure_moonshine_model_download"),
            patch("mystic.cli.ensure_pocket_onnx_models"),
            patch("mystic.cli.ensure_local_model"),
            patch("mystic.cli.asyncio.to_thread", new=AsyncMock(side_effect=_fake_to_thread)),
            patch("mystic.cli.click.echo"),
        ):
            await ensure_dependencies(selections, on_step=_on_step)

        self.assertEqual(
            steps,
            [
                "Checking voice server...",
                "Downloading STT model...",
                "Downloading TTS model...",
                "Downloading embedding model...",
            ],
        )

    async def test_ensure_dependencies_quiet_suppresses_terminal_output(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "medium"},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )

        async def _fake_to_thread(fn: object, *args: object) -> None:
            assert callable(fn)
            fn(*args)

        with (
            patch("mystic.cli.ensure_livekit_binary", new=AsyncMock()),
            patch("mystic.cli.ensure_python_extra", new=AsyncMock()),
            patch("mystic.cli._ensure_turn_detector_model_download"),
            patch("mystic.cli._ensure_moonshine_model_download"),
            patch("mystic.cli.ensure_pocket_onnx_models"),
            patch("mystic.cli.ensure_local_model"),
            patch("mystic.cli.asyncio.to_thread", new=AsyncMock(side_effect=_fake_to_thread)),
            patch("mystic.cli.click.echo") as echo_mock,
        ):
            await ensure_dependencies(selections, quiet=True)

        echo_mock.assert_not_called()


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)

    def tearDown(self) -> None:
        clear_config_cache()
        cleanup_runtime_files()
        self.temp_home.__exit__(None, None, None)

    def test_run_health_reports_turn_detection_degraded_when_assets_missing(self) -> None:
        db = MagicMock()
        with (
            patch("mystic.cli.probe_daemon", return_value={
                "pid": 123,
                "port": 3456,
                "dashboard": "http://localhost:3456/dashboard",
                "started_at": 1000,
            }),
            patch("mystic.cli.check_tailscale_ready", return_value=(True, None)),
            patch("mystic.cli.open_database", return_value=db),
            patch("mystic.cli.close_database"),
            patch("mystic.cli.list_active_calls", return_value=[]),
            patch("mystic.cli.get_todays_calls", return_value=[]),
            patch("mystic.cli.get_all_pending_actions", return_value=[]),
            patch("mystic.cli.get_all_people", return_value=[]),
            patch("mystic.cli.is_python_package_available", return_value=True),
            patch("mystic.cli.turn_detector_assets_missing", return_value=["languages.json"]),
        ):
            payload, exit_code = run_health()

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(exit_code, 1)
        subsystems = cast(dict[str, str], payload["subsystems"])
        self.assertEqual(subsystems["turn_detection"], "degraded")


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_setup_spawns_background_daemon_after_prepare(self) -> None:
        temp_home = TempAppHome()
        home = temp_home.__enter__()
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": ""},
            stt_config={"provider": ""},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key=None,
        )

        mock_agent_config = MagicMock()
        mock_agent_config.server.port = 3456
        fake_db = MagicMock()
        fake_app = MagicMock()
        fake_server = MagicMock()
        fake_server.port = 3456
        fake_server.close = AsyncMock()
        setup_done_event: asyncio.Event | None = None
        daemon_result = {
            "status": "started",
            "pid": 999,
            "port": 3456,
            "dashboard": "http://localhost:3456/dashboard",
        }

        def _fake_set_setup_done_event(event: asyncio.Event | None) -> None:
            nonlocal setup_done_event
            setup_done_event = event

        async def _fake_start_server(*args: object, **kwargs: object) -> object:
            assert setup_done_event is not None
            setup_done_event.set()
            return fake_server

        try:
            with ExitStack() as stack:
                stack.enter_context(patch("mystic.cli.config_exists", return_value=False))
                stack.enter_context(patch("mystic.cli._generate_default_agent_name", return_value="mystic-1"))
                stack.enter_context(patch("mystic.cli._apply_agent_env"))
                stack.enter_context(patch("mystic.cli.get_home", return_value=home))
                stack.enter_context(patch("mystic.cli._default_setup_selections", return_value=selections))
                ensure_livekit_binary_mock = stack.enter_context(
                    patch("mystic.cli.ensure_livekit_binary", new=AsyncMock())
                )
                stack.enter_context(patch("mystic.cli.write_config_files"))
                stack.enter_context(patch("mystic.cli.seed_prompt_files"))
                stack.enter_context(patch("mystic.cli._setup_database"))
                stack.enter_context(patch("mystic.cli.seed_dashboard_defaults"))
                stack.enter_context(patch("mystic.cli.ensure_dashboard_token", return_value="test-token"))
                stack.enter_context(patch("mystic.cli.get_agent_config", return_value=mock_agent_config))
                stack.enter_context(patch("mystic.cli.open_database", return_value=fake_db))
                stack.enter_context(patch("mystic.cli.initialize_schema"))
                close_database_mock = stack.enter_context(patch("mystic.cli.close_database"))
                create_setup_app_mock = stack.enter_context(
                    patch("mystic.cli.create_setup_app", return_value=fake_app)
                )
                stack.enter_context(
                    patch("mystic.cli.set_setup_done_event", side_effect=_fake_set_setup_done_event)
                )
                start_server_mock = stack.enter_context(
                    patch("mystic.cli.start_server", new=AsyncMock(side_effect=_fake_start_server))
                )
                stack.enter_context(patch("mystic.cli.probe_daemon", return_value=None))
                run_start_mock = stack.enter_context(
                    patch("mystic.cli.run_start", return_value=daemon_result)
                )
                stack.enter_context(patch("mystic.cli._open_browser"))
                payload = await run_setup()
        finally:
            temp_home.__exit__(None, None, None)

        ensure_livekit_binary_mock.assert_awaited_once_with()
        create_setup_app_mock.assert_called_once_with(fake_db)
        start_server_mock.assert_awaited_once_with(fake_app, 3456, shutdown_timeout=2.0)
        run_start_mock.assert_called_once_with()
        fake_server.close.assert_awaited_once()
        close_database_mock.assert_called_once_with(fake_db)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["agent"], "mystic-1")
        self.assertEqual(payload["dashboard"], "http://localhost:3456/dashboard")
        self.assertEqual(payload["pid"], 999)

    async def test_run_setup_reuses_existing_daemon_when_available(self) -> None:
        temp_home = TempAppHome()
        home = temp_home.__enter__()
        mock_agent_config = MagicMock()
        mock_agent_config.server.port = 3456
        existing = {
            "pid": 321,
            "port": 3456,
            "dashboard": "http://localhost:3456/dashboard",
        }

        try:
            with ExitStack() as stack:
                stack.enter_context(patch("mystic.cli.config_exists", return_value=True))
                stack.enter_context(patch("mystic.cli._apply_agent_env"))
                stack.enter_context(patch("mystic.cli.get_home", return_value=home))
                stack.enter_context(patch("mystic.cli.seed_dashboard_defaults"))
                stack.enter_context(patch("mystic.cli.ensure_dashboard_token", return_value="test-token"))
                stack.enter_context(patch("mystic.cli.get_agent_config", return_value=mock_agent_config))
                stack.enter_context(patch("mystic.cli.probe_daemon", return_value=existing))
                start_server_mock = stack.enter_context(
                    patch("mystic.cli.start_server", new=AsyncMock())
                )
                open_browser_mock = stack.enter_context(patch("mystic.cli._open_browser"))
                payload = await run_setup(agent_name="mystic-1")
        finally:
            temp_home.__exit__(None, None, None)

        start_server_mock.assert_not_awaited()
        open_browser_mock.assert_called_once_with(
            "http://localhost:3456/dashboard/login?token=test-token&next=/dashboard/setup"
        )
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["pid"], 321)


class InitSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    def test_show_init_summary_includes_next_steps(self) -> None:
        selections = InitSelections(
            timezone="America/Los_Angeles",
            selected_voice_id="Hades",
            server_port=3456,
            livekit_port=7880,
            tts_config={"provider": "pocket"},
            stt_config={"provider": "moonshine", "model": "small"},
            embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
            llm_realtime={"provider": "openrouter", "model": "rt-model"},
            llm_backend={"provider": "openrouter", "model": "backend-model"},
            openrouter_key="openrouter-key",
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            _show_init_summary(selections)

        output = captured.getvalue()
        self.assertIn("Run 'mystic start', then open the dashboard live page to meet your agent.", output)


if __name__ == "__main__":
    unittest.main()
