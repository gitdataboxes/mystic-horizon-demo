from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from typing import cast
from unittest.mock import patch

from mystic.config import (
    AgentConfig,
    DeepgramSttConfig,
    DashboardConfig,
    InworldTtsConfig,
    LocalEmbeddingConfig,
    OAuthTokens,
    UnconfiguredSttConfig,
    UnconfiguredTtsConfig,
    clear_config_cache,
    config_exists,
    ensure_dashboard_token,
    get_agent_config,
    get_backend_llm_config,
    get_calendar_hub_config,
    get_daemon_socket_path,
    get_dashboard_config,
    get_dashboard_history_dir,
    get_dashboard_style_path,
    get_embedding_config,
    get_embedding_dimensions,
    get_hub_tokens,
    get_journal_dir,
    get_providers_config,
    get_recordings_dir,
    get_smtp_config,
    get_setup_status,
    get_realtime_llm_config,
    is_valid_e164,
    list_journal_entries,
    list_dashboard_files,
    load_config,
    load_config_async,
    load_config_fresh,
    read_dashboard_file,
    read_journal_entry,
    save_hub_tokens,
    validate_e164,
    write_dashboard_file,
    write_identity,
    write_soul,
    write_config,
    TwilioDraftConfig,
)
from mystic.types import Identity
from tests.python_helpers import (
    TempAppHome,
    TEST_AGENT_CONFIG,
    TEST_IDENTITY,
    TEST_INTELLIGENCE_CONFIG,
    TEST_PROVIDERS_CONFIG,
    TEST_SOUL,
    seed_core_files,
)


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        clear_config_cache()

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    def test_config_exists_reflects_agent_json(self) -> None:
        self.assertTrue(config_exists())
        (self.home / "config" / "agent.json").unlink()
        clear_config_cache()
        self.assertFalse(config_exists())

    def test_caches_loaded_configs_until_cache_is_cleared(self) -> None:
        first = get_agent_config()
        self.assertEqual(first.agent.name, "TestBot")

        updated = dict(TEST_AGENT_CONFIG)
        updated["agent"] = {"name": "CachedBot"}
        (self.home / "config" / "agent.json").write_text(json.dumps(updated), encoding="utf-8")

        self.assertEqual(get_agent_config().agent.name, "TestBot")
        clear_config_cache("agent.json")
        self.assertEqual(get_agent_config().agent.name, "CachedBot")

    def test_load_config_validates_structure(self) -> None:
        bad = dict(TEST_AGENT_CONFIG)
        bad["owner"] = {"phone": 12345}
        (self.home / "config" / "agent.json").write_text(json.dumps(bad), encoding="utf-8")
        clear_config_cache("agent.json")
        with self.assertRaisesRegex(ValueError, "owner.phone"):
            load_config("agent.json")

    def test_load_config_rejects_unknown_owner_keys(self) -> None:
        bad = dict(TEST_AGENT_CONFIG)
        bad["owner"] = {"phone": "+15551234567", "name": "Legacy"}
        (self.home / "config" / "agent.json").write_text(json.dumps(bad), encoding="utf-8")
        clear_config_cache("agent.json")
        with self.assertRaisesRegex(ValueError, "Unknown keys in owner"):
            load_config("agent.json")

    def test_missing_owner_section_parses_to_default(self) -> None:
        no_owner = dict(TEST_AGENT_CONFIG)
        del no_owner["owner"]
        (self.home / "config" / "agent.json").write_text(json.dumps(no_owner), encoding="utf-8")
        clear_config_cache("agent.json")
        agent = get_agent_config()
        self.assertIsNone(agent.owner.phone)

    def test_missing_tunnel_section_parses_to_enabled_default(self) -> None:
        no_tunnel = dict(TEST_AGENT_CONFIG)
        del no_tunnel["tunnel"]
        (self.home / "config" / "agent.json").write_text(json.dumps(no_tunnel), encoding="utf-8")
        clear_config_cache("agent.json")
        agent = get_agent_config()
        self.assertTrue(agent.tunnel.enabled)

    def test_missing_recording_section_parses_to_disabled_default(self) -> None:
        agent = get_agent_config()
        self.assertFalse(agent.recording.enabled)

    def test_tunnel_enabled_flag_round_trips(self) -> None:
        disabled_tunnel = dict(TEST_AGENT_CONFIG)
        disabled_tunnel["tunnel"] = {"enabled": False}
        (self.home / "config" / "agent.json").write_text(json.dumps(disabled_tunnel), encoding="utf-8")
        clear_config_cache("agent.json")
        agent = get_agent_config()
        self.assertFalse(agent.tunnel.enabled)

    def test_recording_enabled_flag_round_trips(self) -> None:
        enabled_recording = dict(TEST_AGENT_CONFIG)
        enabled_recording["recording"] = {"enabled": True}
        (self.home / "config" / "agent.json").write_text(json.dumps(enabled_recording), encoding="utf-8")
        clear_config_cache("agent.json")
        agent = get_agent_config()
        self.assertTrue(agent.recording.enabled)

    def test_get_recordings_dir_uses_app_home(self) -> None:
        self.assertEqual(get_recordings_dir(), self.home / "recordings")

    def test_dashboard_token_round_trips_when_present(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["dashboard"] = {"token": "tok-123"}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        dashboard = get_dashboard_config()

        self.assertEqual(dashboard, DashboardConfig(token="tok-123"))

    def test_ensure_dashboard_token_persists_new_token(self) -> None:
        token = ensure_dashboard_token()

        self.assertTrue(token)
        clear_config_cache("providers.json")
        persisted = get_dashboard_config()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.token, token)

    def test_write_dashboard_file_creates_history_entry_on_overwrite(self) -> None:
        with patch("mystic.config.time.time", side_effect=[1.0, 2.0]):
            write_dashboard_file("pages/home.html", "<h1>v1</h1>")
            write_dashboard_file("pages/home.html", "<h1>v2</h1>", note="Refined layout")

        self.assertEqual(read_dashboard_file("pages/home.html"), "<h1>v2</h1>")
        self.assertEqual(list_dashboard_files(), ["pages/home.html"])
        history_files = sorted(path.name for path in get_dashboard_history_dir().iterdir())
        self.assertEqual(history_files, ["1000-pages__home.html"])
        history_payload = (get_dashboard_history_dir() / history_files[0]).read_text(encoding="utf-8")
        self.assertIn("trigger: design-dashboard", history_payload)
        self.assertIn("note: Refined layout", history_payload)
        self.assertIn("path: pages/home.html", history_payload)
        self.assertIn("<h1>v1</h1>", history_payload)

    def test_dashboard_paths_use_app_home(self) -> None:
        self.assertEqual(get_dashboard_style_path(), self.home / "dashboard" / "style.css")
        self.assertEqual(get_daemon_socket_path(), self.home / "mystic.sock")

    def test_write_soul_creates_journal_entry(self) -> None:
        with patch("mystic.config.time.time", return_value=1.0):
            write_soul("# Updated Soul\n\nStay sharp.")

        entries = list_journal_entries("soul")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].timestamp, 1000)
        self.assertEqual(entries[0].file_type, "soul")
        self.assertEqual(entries[0].trigger, "write-soul")
        self.assertEqual(entries[0].note, "")
        self.assertEqual(entries[0].content, TEST_SOUL)
        self.assertTrue((get_journal_dir("soul") / "1000.md").exists())

    def test_write_identity_creates_journal_entry(self) -> None:
        identity = Identity(
            name="Lyra",
            creature="owl",
            vibe="calm and observant",
            emoji=":owl:",
        )

        with patch("mystic.config.time.time", return_value=2.0):
            write_identity(identity)

        entries = list_journal_entries("identity")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].timestamp, 2000)
        self.assertEqual(entries[0].file_type, "identity")
        self.assertEqual(entries[0].trigger, "write-identity")
        self.assertEqual(entries[0].content, TEST_IDENTITY)

    def test_first_write_creates_no_journal(self) -> None:
        (self.home / "SOUL.md").unlink()
        (self.home / "IDENTITY.md").unlink()

        write_soul("# First Soul\n")
        write_identity(Identity(name="Nova", creature="fox", vibe="bright", emoji=":fox:"))

        self.assertEqual(list_journal_entries("soul"), [])
        self.assertEqual(list_journal_entries("identity"), [])

    def test_list_journal_entries_newest_first(self) -> None:
        with patch("mystic.config.time.time", side_effect=[1.0, 2.0, 3.0]):
            write_soul("First rewrite")
            write_soul("Second rewrite")
            write_soul("Third rewrite")

        entries = list_journal_entries("soul")

        self.assertEqual([entry.timestamp for entry in entries], [3000, 2000, 1000])
        self.assertEqual([entry.content for entry in entries], ["Second rewrite", "First rewrite", TEST_SOUL])

    def test_list_journal_entries_respects_limit(self) -> None:
        with patch("mystic.config.time.time", side_effect=[1.0, 2.0, 3.0]):
            write_soul("First rewrite")
            write_soul("Second rewrite")
            write_soul("Third rewrite")

        entries = list_journal_entries("soul", limit=2)

        self.assertEqual([entry.timestamp for entry in entries], [3000, 2000])

    def test_read_journal_entry_by_timestamp(self) -> None:
        with patch("mystic.config.time.time", return_value=1.0):
            write_soul("Rewritten soul", trigger="edit-soul", note="Made it warmer")

        entry = read_journal_entry("soul", 1000)

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.timestamp, 1000)
        self.assertEqual(entry.file_type, "soul")
        self.assertEqual(entry.trigger, "edit-soul")
        self.assertEqual(entry.note, "Made it warmer")
        self.assertEqual(entry.content, TEST_SOUL)

    def test_read_journal_entry_not_found(self) -> None:
        self.assertIsNone(read_journal_entry("soul", 9999))

    def test_journal_preserves_trigger_and_note(self) -> None:
        identity = Identity(
            name="Lyra",
            creature="owl",
            vibe="calm and observant",
            emoji=":owl:",
        )

        with patch("mystic.config.time.time", return_value=1.0):
            write_identity(identity, trigger="bootstrap", note="Initial rewrite")

        entry = list_journal_entries("identity")[0]

        self.assertEqual(entry.trigger, "bootstrap")
        self.assertEqual(entry.note, "Initial rewrite")

    def test_embedding_union_parses_local_and_dimension_accessor(self) -> None:
        providers = get_providers_config()
        self.assertIsInstance(providers.embedding, LocalEmbeddingConfig)
        self.assertEqual(get_embedding_dimensions(), 256)

        embedding = get_embedding_config()
        self.assertIsInstance(embedding, LocalEmbeddingConfig)
        self.assertEqual(embedding.model, "nomic-embed-text-v1.5")

    def test_tts_union_parses_inworld_and_round_trips(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["tts"] = {"provider": "inworld", "apiKey": "iw-key", "model": "inworld-tts-1.5-mini"}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        loaded = get_providers_config()

        self.assertIsInstance(loaded.tts, InworldTtsConfig)
        assert isinstance(loaded.tts, InworldTtsConfig)
        self.assertEqual(loaded.tts.apiKey, "iw-key")
        self.assertEqual(loaded.tts.model, "inworld-tts-1.5-mini")

        write_config("providers.json", loaded)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["tts"]["provider"], "inworld")
        self.assertEqual(payload["tts"]["apiKey"], "iw-key")

    def test_stt_union_parses_deepgram_and_round_trips(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["stt"] = {"provider": "deepgram", "apiKey": "dg-key", "model": "nova-3"}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        loaded = get_providers_config()

        self.assertIsInstance(loaded.stt, DeepgramSttConfig)
        assert isinstance(loaded.stt, DeepgramSttConfig)
        self.assertEqual(loaded.stt.apiKey, "dg-key")
        self.assertEqual(loaded.stt.model, "nova-3")

        write_config("providers.json", loaded)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["stt"]["provider"], "deepgram")
        self.assertEqual(payload["stt"]["apiKey"], "dg-key")

    def test_twilio_draft_parses_and_round_trips(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers.pop("twilio", None)
        providers["twilioDraft"] = {"accountSid": "AC-draft", "authToken": "draft-token"}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        loaded = get_providers_config()

        self.assertIsNone(loaded.twilio)
        self.assertIsInstance(loaded.twilioDraft, TwilioDraftConfig)
        assert loaded.twilioDraft is not None
        self.assertEqual(loaded.twilioDraft.accountSid, "AC-draft")

        write_config("providers.json", loaded)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["twilioDraft"], {"accountSid": "AC-draft", "authToken": "draft-token"})

    def test_voice_unions_allow_unconfigured_dashboard_state(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["stt"] = {"provider": ""}
        providers["tts"] = {"provider": ""}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        loaded = get_providers_config()

        self.assertIsInstance(loaded.stt, UnconfiguredSttConfig)
        self.assertIsInstance(loaded.tts, UnconfiguredTtsConfig)

        write_config("providers.json", loaded)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["stt"]["provider"], "")
        self.assertEqual(payload["tts"]["provider"], "")

    async def test_load_config_async_returns_validated_config(self) -> None:
        loaded = await load_config_async("agent.json")
        self.assertIsInstance(loaded, AgentConfig)
        self.assertEqual(loaded.agent.name, "TestBot")

    def test_write_config_validates_and_round_trips(self) -> None:
        agent = get_agent_config()
        updated = asdict(agent)
        updated["agent"]["name"] = "Lyra"
        write_config("agent.json", updated)
        clear_config_cache("agent.json")
        self.assertEqual(get_agent_config().agent.name, "Lyra")

    def test_load_config_fresh_bypasses_cache(self) -> None:
        self.assertEqual(get_agent_config().agent.name, "TestBot")
        updated = dict(TEST_AGENT_CONFIG)
        updated["agent"] = {"name": "FreshBot"}
        (self.home / "config" / "agent.json").write_text(json.dumps(updated), encoding="utf-8")
        self.assertEqual(load_config_fresh("agent.json").agent.name, "FreshBot")

    def test_e164_helpers_accept_and_reject_expected_values(self) -> None:
        self.assertTrue(is_valid_e164("+15551234567"))
        self.assertFalse(is_valid_e164("5551234567"))
        self.assertEqual(validate_e164("+15551234567"), "+15551234567")
        with self.assertRaisesRegex(ValueError, "Invalid E.164"):
            validate_e164("5551234567")

    def test_get_setup_status_reports_core_and_tailscale_state(self) -> None:
        with patch("mystic.config.check_tailscale_ready", return_value=(False, "not installed")):
            status = get_setup_status()

        self.assertTrue(status.identity)
        self.assertTrue(status.soul)
        self.assertFalse(status.tailscale_installed)
        self.assertEqual(status.tailscale_reason, "not installed")
        self.assertTrue(status.twilio)
        self.assertTrue(status.core_complete)

    def test_default_llm_resolution_uses_openrouter(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers.pop("llm", None)
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        realtime = get_realtime_llm_config()
        backend = get_backend_llm_config()

        self.assertEqual(realtime.baseURL, "https://openrouter.ai/api/v1")
        self.assertEqual(realtime.apiKey, "test-openrouter-key")
        self.assertEqual(realtime.model, "openai/gpt-5.5")
        self.assertFalse(backend.isCustom)

    def test_stt_config_rejects_legacy_base_model(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["stt"] = {"provider": "moonshine", "model": "base"}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        with self.assertRaisesRegex(ValueError, "stt.model must be 'tiny', 'small', or 'medium'"):
            get_providers_config()

    def test_custom_llm_resolution_supports_mixed_backends(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["llm"] = {
            "realtime": {
                "provider": "openrouter",
                "model": "openai/gpt-5.4-mini-mini",
            },
            "backend": {
                "provider": "custom",
                "baseURL": "http://localhost:8080/v1",
                "apiKey": "local-key",
            },
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        realtime = get_realtime_llm_config()
        backend = get_backend_llm_config()

        self.assertEqual(realtime.model, "openai/gpt-5.4-mini-mini")
        self.assertEqual(backend.baseURL, "http://localhost:8080/v1")
        self.assertEqual(backend.apiKey, "local-key")
        self.assertTrue(backend.isCustom)

    def test_custom_realtime_requires_model(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["llm"] = {"realtime": {"provider": "custom", "baseURL": "http://localhost:11434/v1"}}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")
        with self.assertRaisesRegex(ValueError, "Realtime LLM slot requires model"):
            get_realtime_llm_config()

    def test_custom_backend_requires_base_url(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["llm"] = {"backend": {"provider": "custom"}}
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")
        with self.assertRaisesRegex(ValueError, "Custom LLM requires baseURL"):
            get_backend_llm_config()

    def test_intelligence_config_parses_expected_models(self) -> None:
        intelligence = load_config("intelligence.json")
        retrieval = cast(dict[str, object], TEST_INTELLIGENCE_CONFIG["retrieval"])
        self.assertEqual(intelligence.retrieval.limit, retrieval["limit"])
        self.assertEqual(intelligence.judgment.owner_call.model, "test/model")

    def test_calendar_hub_config_parses_when_present(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["calendar"] = {
            "subscriptions": [],
            "syncIntervalMinutes": 15,
            "reminderMinutes": 10,
            "hub": {
                "provider": "google",
                "calendarId": "primary",
                "clientId": "client-id",
                "clientSecret": "client-secret",
                "writeEnabled": True,
            },
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        hub = get_calendar_hub_config()

        self.assertIsNotNone(hub)
        assert hub is not None
        self.assertEqual(hub.provider, "google")
        self.assertEqual(hub.calendar_id, "primary")
        self.assertEqual(hub.client_id, "client-id")

    def test_calendar_hub_accessor_returns_none_when_write_disabled(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["calendar"] = {
            "hub": {
                "provider": "caldav",
                "calendarId": "/dav/calendars/test/default/",
                "baseUrl": "https://nextcloud.example.test",
                "username": "alice",
                "password": "secret",
                "writeEnabled": False,
            },
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        self.assertIsNone(get_calendar_hub_config())

    def test_smtp_config_parses_and_serializes_camel_case_fields(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["smtp"] = {
            "host": "smtp.example.com",
            "port": 587,
            "username": "mailer",
            "password": "secret",
            "fromAddress": "agent@example.com",
            "useTls": True,
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

        smtp = get_smtp_config()

        self.assertIsNotNone(smtp)
        assert smtp is not None
        self.assertEqual(smtp.host, "smtp.example.com")
        self.assertEqual(smtp.from_address, "agent@example.com")
        self.assertTrue(smtp.use_tls)

        write_config("providers.json", get_providers_config())
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["smtp"]["fromAddress"], "agent@example.com")
        self.assertTrue(payload["smtp"]["useTls"])

    def test_hub_tokens_round_trip(self) -> None:
        tokens = OAuthTokens(
            access_token="access-123",
            refresh_token="refresh-456",
            expires_at=1_800_000_000,
        )

        save_hub_tokens(tokens)
        loaded = get_hub_tokens()

        self.assertEqual(loaded, tokens)
        self.assertTrue((self.home / "config" / "hub_tokens.json").exists())
