from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import asdict
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import mystic.web as web_module
from mystic.config import get_agent_config, get_intelligence_config, get_providers_config, write_config
from mystic.db import (
    close_database,
    initialize_schema,
    insert_action,
    insert_call,
    insert_game_score,
    get_action_by_id,
    open_database,
    upsert_person,
)
from mystic.server import create_app
from mystic.web import (
    SESSION_COOKIE,
    _maybe_import_sibling_twilio_credentials,
    _update_twilio_settings,
    _update_voice_settings,
    _voice_readiness,
    build_session_cookie,
)
from tests.python_helpers import TempAppHome, seed_core_files

TUNNEL_URL = "https://test-machine.tail1234.ts.net"


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        web_module._prepare_task = None
        web_module.set_setup_done_event(None)
        web_module.set_setup_db(None)
        web_module.set_setup_server(None)
        web_module.set_setup_runtime(None)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        self.app = create_app(self.db, TUNNEL_URL)

    async def asyncTearDown(self) -> None:
        web_module._prepare_task = None
        web_module.set_setup_done_event(None)
        web_module.set_setup_db(None)
        web_module.set_setup_server(None)
        web_module.set_setup_runtime(None)
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def _auth_cookies(self) -> dict[str, str]:
        dashboard = get_providers_config().dashboard
        assert dashboard is not None
        return {SESSION_COOKIE: build_session_cookie(dashboard.token)}

    async def _invoke(
        self,
        method: str,
        path: str,
        *,
        cookies: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {"Host": "localhost"}
        if headers:
            request_headers.update(headers)
        if cookies:
            request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        probe = make_mocked_request(method, path, headers=request_headers, app=self.app)
        match_info = await self.app.router.resolve(probe)
        request = make_mocked_request(
            method, path, headers=request_headers, app=self.app,
            match_info=dict(match_info),
        )
        if form is not None:
            request.post = AsyncMock(return_value=form)  # type: ignore[method-assign]
        return await match_info.handler(request)

    # -- Auth ----------------------------------------------------------------

    async def test_dashboard_page_redirects_without_session(self) -> None:
        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("GET", "/dashboard/page/home")
        self.assertIn("/dashboard/login", str(ctx.exception.location))

    async def test_dashboard_shell_renders_without_setup_nav_when_configured(self) -> None:
        with patch(
            "mystic.web._voice_readiness",
            return_value={"stt_ready": True, "tts_ready": True},
        ):
            response = cast(web.Response, await self._invoke(
                "GET", "/dashboard/page/home", cookies=self._auth_cookies(),
            ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn('id="hud-strip"', response.text)
        self.assertIn('id="chat-sidebar"', response.text)
        self.assertNotIn('href="/dashboard/live"', response.text)
        self.assertIn('href="/dashboard/settings"', response.text)
        self.assertNotIn('href="/dashboard/setup"', response.text)

    async def test_graph_data_includes_agent_hub_links(self) -> None:
        person = upsert_person(self.db, "+15550001111", name="Owner")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
        )
        with self.db:
            self.db.execute(
                "UPDATE calls SET transcript = ? WHERE id = ?",
                ("[0:00] Agent [text]: Let's get to know you.", call.id),
            )

        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/api/graph", cookies=self._auth_cookies(),
        ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        nodes = payload["nodes"]
        links = payload["links"]
        agent_node = next(node for node in nodes if node["id"] == "agent")
        self.assertEqual(agent_node["type"], "agent")
        self.assertEqual(agent_node["label"], "TestBot")
        person_node = next(node for node in nodes if node["id"] == f"p:{person.id}")
        self.assertEqual(person_node["type"], "person")
        self.assertEqual(person_node["label"], "Owner")
        self.assertEqual(person_node["angleIndex"], 0)
        self.assertNotIn(f"c:{call.id}", {node["id"] for node in nodes})
        edge = next(link for link in links if link["target"] == f"p:{person.id}")
        self.assertEqual(edge["source"], "agent")
        self.assertEqual(edge["type"], "strand")
        self.assertEqual(edge["channel"], "dashboard")
        self.assertEqual(edge["channelLabel"], "Dashboard")
        self.assertEqual(edge["modality"], "text")
        self.assertEqual(edge["modalityLabel"], "Text")
        self.assertEqual(edge["strandLabel"], "Dashboard chat")
        self.assertEqual(edge["total"], 1)
        self.assertIn("meta", payload)

    async def test_graph_data_splits_dashboard_text_and_voice_strands(self) -> None:
        person = upsert_person(self.db, "+15550001111", name="Owner")
        insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
        )
        insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="voice",
        )

        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/api/graph", cookies=self._auth_cookies(),
        ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        strands = {
            (link["channel"], link["modality"]): link
            for link in payload["links"]
            if link["target"] == f"p:{person.id}"
        }
        self.assertEqual(set(strands), {("dashboard", "text"), ("dashboard", "voice")})
        self.assertEqual(strands[("dashboard", "text")]["strandLabel"], "Dashboard chat")
        self.assertEqual(strands[("dashboard", "voice")]["strandLabel"], "Dashboard voice")

    async def test_graph_thread_returns_channel_interactions(self) -> None:
        person = upsert_person(self.db, "+15550001111", name="Owner")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
        )
        with self.db:
            self.db.execute(
                "UPDATE calls SET transcript = ?, summary = ? WHERE id = ?",
                ("[0:00] Owner [text]: Can you remember this?", "Memory check", call.id),
        )

        response = cast(web.Response, await self._invoke(
            "GET", f"/dashboard/api/graph/thread/{person.id}/dashboard/text", cookies=self._auth_cookies(),
        ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["channel"], "dashboard")
        self.assertEqual(payload["channelLabel"], "Dashboard")
        self.assertEqual(payload["modality"], "text")
        self.assertEqual(payload["modalityLabel"], "Text")
        self.assertEqual(payload["strandLabel"], "Dashboard chat")
        self.assertEqual(payload["calls"][0]["id"], call.id)
        self.assertEqual(payload["calls"][0]["summary"], "Memory check")
        self.assertIn("Can you remember this?", payload["calls"][0]["transcriptPreview"])

    async def test_graph_agent_node_detail_uses_identity(self) -> None:
        person = upsert_person(self.db, "+15550001111", name="Owner")
        insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
        )

        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/api/graph/node/agent/agent", cookies=self._auth_cookies(),
        ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["type"], "agent")
        self.assertEqual(payload["name"], "TestBot")
        self.assertEqual(payload["creature"], "digital assistant")
        self.assertEqual(payload["vibe"], "helpful and precise")
        self.assertEqual(payload["personCount"], 1)
        self.assertEqual(payload["callCount"], 1)

    async def test_dashboard_page_redirects_to_setup_when_llm_unconfigured(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload["llm"] = {}
        write_config("providers.json", providers_payload)

        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("GET", "/dashboard/page/home", cookies=self._auth_cookies())
        self.assertEqual(ctx.exception.location, "/dashboard/setup")

    async def test_dashboard_page_redirects_to_setup_when_voice_unconfigured(self) -> None:
        _update_voice_settings({
            "stt_provider": "",
            "tts_provider": "",
        })

        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("GET", "/dashboard/page/home", cookies=self._auth_cookies())
        self.assertEqual(ctx.exception.location, "/dashboard/setup")

    async def test_setup_redirects_without_session(self) -> None:
        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("GET", "/dashboard/setup")
        self.assertIn("/dashboard/login", str(ctx.exception.location))

    # -- Login POST ----------------------------------------------------------

    async def test_login_invalid_token_renders_form(self) -> None:
        response = cast(web.Response, await self._invoke("GET", "/dashboard/login?token=bad-token"))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Login", response.text)

    async def test_login_valid_token_sets_cookie_and_redirects(self) -> None:
        dashboard = get_providers_config().dashboard
        assert dashboard is not None
        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("GET", f"/dashboard/login?token={dashboard.token}")
        self.assertIn(SESSION_COOKIE, ctx.exception.cookies)
        self.assertEqual(ctx.exception.location, "/dashboard/page/home")

    # -- Logout --------------------------------------------------------------

    async def test_logout_clears_cookie_and_redirects(self) -> None:
        with self.assertRaises(web.HTTPFound) as ctx:
            await self._invoke("POST", "/dashboard/logout")
        self.assertEqual(ctx.exception.location, "/dashboard/login")

    # -- Setup page ----------------------------------------------------------

    async def test_setup_renders_current_provider_state(self) -> None:
        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/setup", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Get Started", response.text)
        self.assertIn('class="setup-shell"', response.text)
        self.assertIn('class="setup-main"', response.text)
        self.assertIn('data-llm-mode="cloud"', response.text)
        self.assertIn('data-stt-mode="local"', response.text)
        self.assertIn('data-tts-mode="local"', response.text)
        self.assertIn('data-tts-voice="Olivia"', response.text)
        self.assertIn('value="test-openrouter-key"', response.text)
        self.assertIn('value="http://localhost:1234/v1"', response.text)
        self.assertNotIn('class="app-shell"', response.text)
        self.assertNotIn('<aside class="sidebar">', response.text)
        self.assertNotIn("Log Out", response.text)

    async def test_setup_post_rejects_missing_cloud_keys(self) -> None:
        response = cast(web.Response, await self._invoke(
            "POST",
            "/dashboard/setup",
            cookies=self._auth_cookies(),
            form={
                "llm_mode": "cloud",
                "stt_mode": "cloud",
                "tts_mode": "cloud",
                "openrouter_key": "",
                "deepgram_key": "",
                "inworld_key": "",
                "tts_voice": "Hades",
            },
        ))
        self.assertEqual(response.status, 422)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["errors"]["openrouter_key"], "OpenRouter API key is required for cloud LLM.")
        self.assertEqual(payload["errors"]["deepgram_key"], "Deepgram API key is required for cloud STT.")
        self.assertEqual(payload["errors"]["inworld_key"], "Inworld API key is required for cloud TTS.")

    async def test_setup_post_saves_local_llm_and_cloud_voice_choices(self) -> None:
        response = cast(web.Response, await self._invoke(
            "POST",
            "/dashboard/setup",
            cookies=self._auth_cookies(),
            form={
                "llm_mode": "local",
                "llm_model": "local-model",
                "llm_local_url": "http://localhost:1234/v1/",
                "stt_mode": "cloud",
                "deepgram_key": "dg-live",
                "tts_mode": "cloud",
                "inworld_key": "iw-live",
                "tts_voice": "Olivia",
            },
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertTrue(json.loads(response.text)["ok"])

        providers = get_providers_config()
        agent = get_agent_config()
        intelligence = get_intelligence_config()

        assert providers.llm is not None
        assert providers.llm.realtime is not None
        assert providers.llm.backend is not None
        self.assertEqual(providers.llm.realtime.provider, "custom")
        self.assertEqual(providers.llm.realtime.baseURL, "http://localhost:1234/v1")
        self.assertEqual(providers.llm.realtime.model, "local-model")
        self.assertEqual(providers.llm.backend.provider, "custom")
        self.assertEqual(providers.llm.backend.baseURL, "http://localhost:1234/v1")
        self.assertEqual(providers.llm.backend.model, "local-model")
        self.assertEqual(getattr(providers.stt, "provider", ""), "deepgram")
        self.assertEqual(getattr(providers.stt, "apiKey", ""), "dg-live")
        self.assertEqual(getattr(providers.tts, "provider", ""), "inworld")
        self.assertEqual(getattr(providers.tts, "apiKey", ""), "iw-live")
        self.assertEqual(agent.agent.voiceId, "Olivia")
        self.assertEqual(intelligence.search.model, "local-model")
        self.assertEqual(intelligence.extraction.facts.model, "local-model")

    async def test_setup_post_saves_cloud_llm_and_local_models(self) -> None:
        response = cast(web.Response, await self._invoke(
            "POST",
            "/dashboard/setup",
            cookies=self._auth_cookies(),
            form={
                "llm_mode": "cloud",
                "llm_model": "openai/gpt-5.4-mini",
                "openrouter_key": "or-live",
                "stt_mode": "local",
                "tts_mode": "local",
                "tts_voice": "Hades",
            },
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertTrue(json.loads(response.text)["ok"])

        providers = get_providers_config()
        assert providers.llm is not None
        assert providers.llm.realtime is not None
        assert providers.llm.backend is not None
        self.assertEqual(providers.llm.realtime.provider, "openrouter")
        self.assertEqual(providers.llm.realtime.model, "openai/gpt-5.4-mini")
        self.assertEqual(providers.llm.backend.provider, "openrouter")
        self.assertEqual(providers.llm.backend.model, "openai/gpt-5.4-mini")
        self.assertEqual(providers.openrouter.apiKey if providers.openrouter else "", "or-live")
        self.assertEqual(getattr(providers.stt, "provider", ""), "moonshine")
        self.assertEqual(getattr(providers.stt, "model", ""), "medium")
        self.assertEqual(getattr(providers.tts, "provider", ""), "pocket")

    # -- Settings page -------------------------------------------------------

    async def test_settings_renders_tailscale_status(self) -> None:
        with (
            patch("mystic.http.check_tailscale_ready", return_value=(False, "not installed")),
            patch(
                "mystic.web._voice_readiness",
                return_value={
                    "stt_provider": "moonshine",
                    "tts_provider": "pocket",
                    "stt_ready": True,
                    "tts_ready": True,
                    "embedding_ready": True,
                },
            ),
        ):
            response = cast(web.Response, await self._invoke(
                "GET", "/dashboard/settings", cookies=self._auth_cookies(),
            ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Settings", response.text)
        self.assertIn('<select name="voice_id" id="voice-id">', response.text)
        self.assertIn('<option value="Olivia" selected>Olivia (British Female)</option>', response.text)
        self.assertIn('href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer"', response.text)

    def test_maybe_import_sibling_twilio_credentials_writes_draft(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        write_config("providers.json", providers_payload)

        with (
            patch("mystic.cli.discover_siblings", return_value=["sister"]),
            patch(
                "mystic.cli.extract_sibling_keys",
                return_value={"twilioSid": "AC-sib", "twilioToken": "tok-sib"},
            ),
        ):
            imported = _maybe_import_sibling_twilio_credentials()

        self.assertTrue(imported)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["twilioDraft"], {"accountSid": "AC-sib", "authToken": "tok-sib"})
        self.assertNotIn("twilio", payload)

    def test_maybe_import_sibling_twilio_credentials_skips_when_already_set(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload["twilioDraft"] = {"accountSid": "AC-have", "authToken": "tok-have"}
        write_config("providers.json", providers_payload)

        with (
            patch("mystic.cli.discover_siblings", return_value=["sister"]),
            patch(
                "mystic.cli.extract_sibling_keys",
                return_value={"twilioSid": "AC-sib", "twilioToken": "tok-sib"},
            ) as extract_mock,
        ):
            imported = _maybe_import_sibling_twilio_credentials()

        self.assertFalse(imported)
        extract_mock.assert_not_called()

    def test_maybe_import_sibling_twilio_credentials_scans_for_twilio_keys(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        write_config("providers.json", providers_payload)

        def extract_keys(sibling: str) -> dict[str, str]:
            if sibling == "no-phone":
                return {"openrouter": "or-sib"}
            return {"twilioSid": "AC-phone", "twilioToken": "tok-phone"}

        with (
            patch("mystic.cli.discover_siblings", return_value=["no-phone", "phone"]),
            patch("mystic.cli.extract_sibling_keys", side_effect=extract_keys),
        ):
            imported = _maybe_import_sibling_twilio_credentials()

        self.assertTrue(imported)
        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["twilioDraft"], {"accountSid": "AC-phone", "authToken": "tok-phone"})

    def test_maybe_import_sibling_twilio_credentials_skips_when_no_siblings(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        write_config("providers.json", providers_payload)

        with (
            patch("mystic.cli.discover_siblings", return_value=[]),
            patch("mystic.cli.extract_sibling_keys") as extract_mock,
        ):
            imported = _maybe_import_sibling_twilio_credentials()

        self.assertFalse(imported)
        extract_mock.assert_not_called()

    async def test_settings_renders_twilio_draft_sid(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC-draft", "authToken": "draft-token"}
        write_config("providers.json", providers_payload)

        with (
            patch("mystic.http.check_tailscale_ready", return_value=(True, "")),
            patch(
                "mystic.web._voice_readiness",
                return_value={
                    "stt_provider": "moonshine",
                    "tts_provider": "pocket",
                    "stt_ready": True,
                    "tts_ready": True,
                    "embedding_ready": True,
                },
            ),
        ):
            response = cast(web.Response, await self._invoke(
                "GET", "/dashboard/settings", cookies=self._auth_cookies(),
            ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn('name="twilio_sid" value="AC-draft"', response.text)
        self.assertIn("Credentials saved", response.text)

    def test_update_twilio_settings_saves_draft_without_phone(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        write_config("providers.json", providers_payload)

        _update_twilio_settings({
            "twilio_sid": "AC-new",
            "twilio_auth_token": "new-token",
            "twilio_phone": "",
        })

        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertNotIn("twilio", payload)
        self.assertEqual(payload["twilioDraft"], {"accountSid": "AC-new", "authToken": "new-token"})
        providers = get_providers_config()
        self.assertIsNone(providers.twilio)
        assert providers.twilioDraft is not None
        self.assertEqual(providers.twilioDraft.accountSid, "AC-new")

    def test_update_voice_settings_preserves_twilio_draft(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC-draft", "authToken": "draft-token"}
        write_config("providers.json", providers_payload)

        _update_voice_settings({
            "stt_provider": "deepgram",
            "deepgram_key": "dg-key",
            "tts_provider": "inworld",
            "inworld_key": "iw-key",
        })

        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["twilioDraft"], {"accountSid": "AC-draft", "authToken": "draft-token"})

    def test_update_twilio_settings_promotes_draft_when_phone_added(self) -> None:
        providers_payload = asdict(get_providers_config())
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC-draft", "authToken": "draft-token"}
        write_config("providers.json", providers_payload)

        _update_twilio_settings({
            "twilio_sid": "AC-draft",
            "twilio_auth_token": "",
            "twilio_phone": "+15550006666",
        })

        payload = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload["twilio"],
            {
                "accountSid": "AC-draft",
                "authToken": "draft-token",
                "phoneNumber": "+15550006666",
            },
        )
        self.assertNotIn("twilioDraft", payload)

    def test_update_voice_settings_persists_provider_choices(self) -> None:
        _update_voice_settings({
            "stt_provider": "deepgram",
            "deepgram_key": "dg-key",
            "tts_provider": "inworld",
            "inworld_key": "iw-key",
        })

        providers = get_providers_config()
        self.assertEqual(getattr(providers.stt, "provider", ""), "deepgram")
        self.assertEqual(getattr(providers.stt, "apiKey", ""), "dg-key")
        self.assertEqual(getattr(providers.tts, "provider", ""), "inworld")
        self.assertEqual(getattr(providers.tts, "apiKey", ""), "iw-key")

    def test_voice_readiness_reports_unconfigured_provider_state(self) -> None:
        _update_voice_settings({
            "stt_provider": "",
            "tts_provider": "",
        })

        with patch("mystic.web.embedding_model_missing", return_value=[]):
            readiness = _voice_readiness()

        self.assertEqual(readiness["stt_provider"], "")
        self.assertEqual(readiness["tts_provider"], "")
        self.assertFalse(cast(bool, readiness["stt_ready"]))
        self.assertFalse(cast(bool, readiness["tts_ready"]))
        self.assertTrue(cast(bool, readiness["embedding_ready"]))

    async def test_dashboard_voice_release_ignores_duplicate_participant_disconnect(self) -> None:
        session = web_module.DashboardVoiceSession(
            call_id="call-browser-1",
            room_name="owner-test-room",
            person_id="person-owner",
            date_key=web_module._dashboard_voice_date_key(),
            participant_count=2,
            participant_names={"old-page", "new-page"},
        )
        self.app[web_module._DASHBOARD_VOICE_SESSION_KEY] = session

        await web_module.release_dashboard_voice_session(
            self.app,
            session,
            participant_name="old-page",
        )
        await web_module.release_dashboard_voice_session(
            self.app,
            session,
            participant_name="old-page",
        )

        self.assertEqual(session.participant_names, {"new-page"})
        self.assertEqual(session.participant_count, 1)
        self.assertIsNone(session.idle_task)

    async def test_dashboard_voice_release_ignores_legacy_disconnect_for_named_session(self) -> None:
        session = web_module.DashboardVoiceSession(
            call_id="call-browser-1",
            room_name="owner-test-room",
            person_id="person-owner",
            date_key=web_module._dashboard_voice_date_key(),
            participant_count=1,
            participant_names={"new-page"},
        )
        self.app[web_module._DASHBOARD_VOICE_SESSION_KEY] = session

        await web_module.release_dashboard_voice_session(self.app, session)

        self.assertEqual(session.participant_names, {"new-page"})
        self.assertEqual(session.participant_count, 1)
        self.assertIsNone(session.idle_task)

    async def test_voice_token_hydrates_sidebar_from_persistent_chat_call(self) -> None:
        person = upsert_person(self.db, "+15550007777", "Owner")
        live_call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="voice",
        )
        chat_call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
            call_id="dashboard-chat",
        )
        with self.db:
            self.db.execute(
                "UPDATE calls SET transcript = ? WHERE id = ?",
                ("[0:00] Agent [text]: This should not hydrate.", live_call.id),
            )
            self.db.execute(
                "UPDATE calls SET transcript = ? WHERE id = ?",
                (
                    "[0:00] Caller [text]: Are you still there?\n"
                    '[0:01] Tool [event]: {"type":"tool_started","name":"read-calendar"}\n'
                    "[0:02] Agent [text]: Yes.",
                    chat_call.id,
                ),
            )

        session = web_module.DashboardVoiceSession(
            call_id=live_call.id,
            room_name="owner-test-room",
            person_id=person.id,
            date_key=web_module._dashboard_voice_date_key(),
        )

        with (
            patch("mystic.web.acquire_dashboard_voice_session", new=AsyncMock(return_value=session)),
            patch("mystic.web.generate_token", new=AsyncMock(return_value="token")),
            patch("mystic.web.broadcast", new=AsyncMock()),
        ):
            response = cast(web.Response, await self._invoke(
                "POST", "/dashboard/api/voice/token", cookies=self._auth_cookies(),
            ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["callId"], live_call.id)
        self.assertEqual(payload["chatCallId"], "dashboard-chat")
        self.assertEqual(
            payload["hudHistory"],
            [{"speaker": "agent", "text": "This should not hydrate.", "modality": "text"}],
        )
        self.assertEqual(
            payload["history"],
            [
                {"speaker": "user", "text": "Are you still there?", "modality": "text"},
                {"type": "tool_started", "name": "read-calendar"},
                {"speaker": "agent", "text": "Yes.", "modality": "text"},
            ],
        )

    async def test_game_scores_returns_saved_leaderboard_scores(self) -> None:
        insert_game_score(self.db, name="AAA", score=1200, wave=3)
        insert_game_score(self.db, name="BBB", score=2400, wave=5)

        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/api/game/scores", cookies=self._auth_cookies(),
        ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(
            [(row["name"], row["score"], row["wave"]) for row in payload["scores"]],
            [("BBB", 2400, 5), ("AAA", 1200, 3)],
        )

    async def test_game_token_creates_ephemeral_room_with_kind_game(self) -> None:
        create_calls: list[tuple[str, dict[str, object]]] = []

        async def fake_create_room(livekit_config, room_name, metadata):
            create_calls.append((room_name, dict(metadata)))

        with (
            patch(
                "mystic.web._create_dashboard_room_with_agent_dispatch",
                new=AsyncMock(side_effect=fake_create_room),
            ),
            patch("mystic.web.generate_token", new=AsyncMock(return_value="game-token")),
        ):
            response = cast(web.Response, await self._invoke(
                "POST", "/dashboard/api/game/token", cookies=self._auth_cookies(),
            ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["token"], "game-token")
        self.assertTrue(payload["roomName"].startswith("game-"))
        self.assertTrue(payload["participantName"].startswith("pilot-"))

        self.assertEqual(len(create_calls), 1)
        created_room, created_metadata = create_calls[0]
        self.assertEqual(created_room, payload["roomName"])
        self.assertEqual(created_metadata["kind"], "game")
        self.assertEqual(created_metadata["channel"], "dashboard")
        self.assertNotIn("callId", created_metadata)

    async def test_game_token_rooms_are_unique_per_request(self) -> None:
        rooms: list[str] = []

        async def fake_create_room(livekit_config, room_name, metadata):
            rooms.append(room_name)

        with (
            patch(
                "mystic.web._create_dashboard_room_with_agent_dispatch",
                new=AsyncMock(side_effect=fake_create_room),
            ),
            patch("mystic.web.generate_token", new=AsyncMock(return_value="tkn")),
        ):
            await self._invoke("POST", "/dashboard/api/game/token", cookies=self._auth_cookies())
            await self._invoke("POST", "/dashboard/api/game/token", cookies=self._auth_cookies())

        self.assertEqual(len(rooms), 2)
        self.assertNotEqual(rooms[0], rooms[1])

    async def test_voice_token_reports_voice_session_failure(self) -> None:
        with patch(
            "mystic.web.acquire_dashboard_voice_session",
            new=AsyncMock(side_effect=RuntimeError("Room creation failed")),
        ):
            with self.assertRaises(web.HTTPServiceUnavailable) as ctx:
                await self._invoke(
                    "POST", "/dashboard/api/voice/token", cookies=self._auth_cookies(),
                )

        self.assertEqual(ctx.exception.status, 503)
        assert ctx.exception.text is not None
        self.assertIn("Room creation failed", ctx.exception.text)

    async def test_dashboard_voice_session_keeps_room_when_dispatch_assignment_is_pending(self) -> None:
        with (
            patch("mystic.web.create_named_room", new=AsyncMock()) as create_room,
            patch(
                "mystic.web.verify_dispatch_assignment",
                new=AsyncMock(return_value=False),
            ) as verify_dispatch,
            patch("mystic.web.delete_room", new=AsyncMock()) as delete_room,
            patch("mystic.web.broadcast", new=AsyncMock()),
        ):
            session = await web_module._create_dashboard_voice_session(self.db)

        self.assertIsInstance(session, web_module.DashboardVoiceSession)
        self.assertEqual(create_room.await_count, 1)
        verify_dispatch.assert_awaited_once()
        delete_room.assert_not_awaited()
        await_args = create_room.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        metadata = await_args.args[2]
        self.assertEqual(metadata["modality"], "voice")

    async def test_dashboard_voice_refresh_keeps_bootstrap_text_modality(self) -> None:
        (self.home / "IDENTITY.md").unlink()
        person = upsert_person(self.db, "+15551234567", "Owner")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="owner",
            channel="dashboard",
            modality="text",
        )
        session = web_module.DashboardVoiceSession(
            call_id=call.id,
            room_name="owner-test-room",
            person_id=person.id,
            date_key=web_module._dashboard_voice_date_key(),
        )

        with (
            patch("mystic.web.room_has_active_agent", new=AsyncMock(return_value=False)),
            patch("mystic.web.create_named_room", new=AsyncMock()) as create_room,
            patch("mystic.web.verify_dispatch_assignment", new=AsyncMock(return_value=True)),
            patch("mystic.web.delete_room", new=AsyncMock()),
        ):
            await web_module._refresh_dashboard_voice_room(self.db, session)

        await_args = create_room.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        metadata = await_args.args[2]
        self.assertEqual(metadata["modality"], "text")
        self.assertTrue(metadata["bootstrap"])

    async def test_prepare_fragment_starts_background_task(self) -> None:
        fake_task = MagicMock()
        def _fake_create_task(coro: object, *args: object, **kwargs: object) -> object:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return fake_task

        with patch("mystic.web.asyncio.create_task", side_effect=_fake_create_task) as create_task_mock:
            response = cast(web.Response, await self._invoke(
                "POST", "/dashboard/f/prepare", cookies=self._auth_cookies(),
            ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Setting up voice", response.text)
        create_task_mock.assert_called_once()
        self.assertIs(web_module._prepare_task, fake_task)

    async def test_run_prepare_dependencies_broadcasts_step_events(self) -> None:
        async def _fake_ensure_dependencies(*args: object, **kwargs: object) -> None:
            on_step = kwargs.get("on_step")
            self.assertTrue(callable(on_step))
            self.assertTrue(kwargs.get("quiet"))
            assert on_step is not None
            await on_step("Downloading embedding model...")  # type: ignore[misc]

        web_module._prepare_task = MagicMock()
        setup_done = asyncio.Event()
        web_module.set_setup_done_event(setup_done)
        web_module.set_setup_db(self.db)
        broadcast_mock = AsyncMock()
        with (
            patch("mystic.cli.ensure_dependencies", new=AsyncMock(side_effect=_fake_ensure_dependencies)),
            patch("mystic.web.broadcast", new=broadcast_mock),
        ):
            await web_module._run_prepare_dependencies()

        broadcast_mock.assert_has_awaits([
            call("prepare.started", {"status": "started"}),
            call("prepare.step", {"label": "Downloading embedding model..."}),
            call("prepare.done", {"status": "ready"}),
        ])
        self.assertTrue(setup_done.is_set())
        self.assertIsNone(web_module._prepare_task)

    async def test_write_sse_treats_disconnect_as_closed_stream(self) -> None:
        response = AsyncMock()
        response.write.side_effect = ConnectionResetError()

        ok = await web_module._write_sse(response, "event: activity\n\n")

        self.assertFalse(ok)
        response.write.assert_awaited_once_with(b"event: activity\n\n")

    # -- Fragment: calls -----------------------------------------------------

    async def test_fragment_calls_returns_table(self) -> None:
        person = upsert_person(self.db, "+15550001111", "Alice")
        insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/f/calls", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("<table>", response.text)
        self.assertIn("Phone call", response.text)
        self.assertIn("Inbound", response.text)

    # -- Fragment: people ----------------------------------------------------

    async def test_fragment_people_returns_table(self) -> None:
        upsert_person(self.db, "+15550002222", "Bob")
        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/f/people", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Bob", response.text)
        self.assertIn("<table>", response.text)

    # -- Fragment: actions ---------------------------------------------------

    async def test_fragment_actions_returns_table(self) -> None:
        person = upsert_person(self.db, "+15550003333", "Carol")
        insert_action(self.db, intent="Call Carol", source="owner", person_id=person.id)
        response = cast(web.Response, await self._invoke(
            "GET", "/dashboard/f/actions", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Call Carol", response.text)

    # -- Fragment: action detail / 404 ---------------------------------------

    async def test_fragment_action_detail(self) -> None:
        person = upsert_person(self.db, "+15550004444", "Dave")
        action = insert_action(self.db, intent="Email Dave", source="owner", person_id=person.id)
        response = cast(web.Response, await self._invoke(
            "GET", f"/dashboard/f/action/{action.id}", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Email Dave", response.text)

    async def test_fragment_action_404(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            await self._invoke(
                "GET", "/dashboard/f/action/nonexistent-id", cookies=self._auth_cookies(),
            )

    # -- Mutation: action complete -------------------------------------------

    async def test_action_complete(self) -> None:
        person = upsert_person(self.db, "+15550005555", "Eve")
        action = insert_action(self.db, intent="Follow up", source="owner", person_id=person.id)
        response = cast(web.Response, await self._invoke(
            "POST", f"/dashboard/f/action/{action.id}/complete", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "completed")
        updated = get_action_by_id(self.db, action.id)
        assert updated is not None
        self.assertEqual(updated.status, "completed")

    # -- Mutation: action cancel ---------------------------------------------

    async def test_action_cancel(self) -> None:
        person = upsert_person(self.db, "+15550006666", "Frank")
        action = insert_action(self.db, intent="Send report", source="owner", person_id=person.id)
        response = cast(web.Response, await self._invoke(
            "POST", f"/dashboard/f/action/{action.id}/cancel", cookies=self._auth_cookies(),
        ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "cancelled")
        updated = get_action_by_id(self.db, action.id)
        assert updated is not None
        self.assertEqual(updated.status, "cancelled")

    # -- Static asset --------------------------------------------------------

    async def test_static_htmx_serves_file(self) -> None:
        response = cast(web.Response, await self._invoke("GET", "/static/htmx.min.js"))
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.content_type)

    async def test_static_missing_returns_404(self) -> None:
        with self.assertRaises(web.HTTPNotFound):
            await self._invoke("GET", "/static/does-not-exist.js")

    # -- Agent-editable page -------------------------------------------------

    async def test_dashboard_page_renders(self) -> None:
        pages_dir = self.home / "dashboard" / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        (pages_dir / "home.html").write_text("<p>Welcome home</p>", encoding="utf-8")
        with patch(
            "mystic.web._voice_readiness",
            return_value={"stt_ready": True, "tts_ready": True},
        ):
            response = cast(web.Response, await self._invoke(
                "GET", "/dashboard/page/home", cookies=self._auth_cookies(),
            ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Welcome home", response.text)

    async def test_dashboard_page_hx_request_returns_content_only(self) -> None:
        pages_dir = self.home / "dashboard" / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        (pages_dir / "home.html").write_text("<p>Welcome home</p>", encoding="utf-8")
        with patch(
            "mystic.web._voice_readiness",
            return_value={"stt_ready": True, "tts_ready": True},
        ):
            response = cast(web.Response, await self._invoke(
                "GET",
                "/dashboard/page/home",
                cookies=self._auth_cookies(),
                headers={"HX-Request": "true"},
            ))
        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertEqual(response.text, "<p>Welcome home</p>")

    async def test_dashboard_page_404(self) -> None:
        with (
            patch(
                "mystic.web._voice_readiness",
                return_value={"stt_ready": True, "tts_ready": True},
            ),
            self.assertRaises(web.HTTPNotFound),
        ):
            await self._invoke(
                "GET", "/dashboard/page/nonexistent", cookies=self._auth_cookies(),
            )


if __name__ == "__main__":
    unittest.main()
