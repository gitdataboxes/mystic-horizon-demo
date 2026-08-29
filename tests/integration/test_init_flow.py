from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from mystic.cli import InitSelections, run_connect_twilio, run_init, write_config_files
from mystic.config import clear_config_cache, get_agent_config, get_providers_config
from mystic.db import close_database, open_database
from tests.python_helpers import TempAppHome


async def test_run_init_writes_config_seed_files_and_bootstrap_action() -> None:
    temp_home = TempAppHome()
    home = temp_home.__enter__()
    selections = InitSelections(
        timezone="America/Los_Angeles",
        selected_voice_id="Hades",
        server_port=3456,
        livekit_port=7880,
        tts_config={"provider": "pocket"},
        stt_config={"provider": "moonshine", "model": "small"},
        embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
        llm_realtime={"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
        llm_backend={"provider": "local", "baseURL": "http://127.0.0.1:11434/v1", "model": "llama3"},
        openrouter_key="openrouter-key",
        owner_phone="+15551234567",
    )

    try:
        with (
            patch("mystic.cli._maybe_import_sibling_keys", return_value={}),
            patch("mystic.cli._prompt_quick_init", return_value=selections),
            patch("mystic.cli.ensure_dependencies", new=AsyncMock()),
            patch("mystic.cli.click.confirm", return_value=False),
        ):
            await run_init()

        clear_config_cache()
        agent = get_agent_config()
        providers = get_providers_config()
        assert agent.owner.phone == "+15551234567"
        assert agent.server.port == 3456
        assert agent.agent.voiceId == "Hades"
        assert providers.livekit.port == 7880
        assert getattr(providers.tts, "provider", "") == "pocket"
        assert (home / "prompts" / "shared" / "context.md").exists()
        assert (home / "faq" / "about-me.md").exists()
        assert (home / "migrations" / "001.sql").exists()
        assert not (home / "IDENTITY.md").exists()
        assert not (home / "SOUL.md").exists()

        db = open_database()
        try:
            rows = db.execute("SELECT intent, status FROM actions").fetchall()
        finally:
            close_database(db)
        assert [dict(row) for row in rows] == [
            {"intent": "Get to know owner", "status": "pending"}
        ]
    finally:
        temp_home.__exit__(None, None, None)


async def test_run_connect_twilio_updates_existing_provider_and_tunnel_config() -> None:
    temp_home = TempAppHome()
    home = temp_home.__enter__()
    selections = InitSelections(
        timezone="America/Los_Angeles",
        selected_voice_id="Mark",
        server_port=3456,
        livekit_port=7880,
        tts_config={"provider": "pocket"},
        stt_config={"provider": "moonshine", "model": "small"},
        embedding_config={"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
        llm_realtime={"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
        llm_backend={"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
        openrouter_key="openrouter-key",
        owner_phone="+15551234567",
    )

    try:
        write_config_files(home, selections)
        with (
            patch("mystic.cli._maybe_import_sibling_keys", return_value={}),
            patch("mystic.cli._prompt_secret", side_effect=["AC123", "auth-token"]),
            patch("mystic.cli.check_tailscale_ready", return_value=(True, "")),
            patch("mystic.cli.click.confirm", return_value=False),
            patch(
                "mystic.cli.click.prompt",
                side_effect=[
                    "+15553334444",
                    "PN123",
                ],
            ),
        ):
            assert await run_connect_twilio(show_intro=False) is True

        providers_payload = json.loads((home / "config" / "providers.json").read_text(encoding="utf-8"))
        agent_payload = json.loads((home / "config" / "agent.json").read_text(encoding="utf-8"))
        assert providers_payload["twilio"] == {
            "accountSid": "AC123",
            "authToken": "auth-token",
            "phoneNumber": "+15553334444",
            "phoneNumberSid": "PN123",
        }
        assert agent_payload["tunnel"] == {
            "enabled": True,
        }
    finally:
        temp_home.__exit__(None, None, None)
