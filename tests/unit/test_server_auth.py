from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import AsyncMock, patch

from aiohttp import WSMsgType
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from mystic.calls import get_active_call_count, reset_active_calls
from mystic.config import get_providers_config
from mystic.db import get_call_by_external_id, insert_call, close_database, initialize_schema, open_database, upsert_person
from mystic.server import (
    build_authenticated_media_stream_url,
    check_rate_limit,
    clear_rate_limit_store,
    create_app,
    create_webhook_handler,
    validate_media_stream_request,
)
from mystic.web import SESSION_COOKIE, build_session_cookie
from tests.python_helpers import TempAppHome, seed_core_files

PUBLIC_PHONE = "+15550001111"
TUNNEL_URL = "https://test-machine.tail1234.ts.net"


class MediaAuthTests(unittest.TestCase):
    def test_builds_signed_websocket_url_that_validates(self) -> None:
        now = 1_700_000_000_000
        secret = "test-secret"

        ws_url = build_authenticated_media_stream_url(
            TUNNEL_URL,
            "call-123",
            secret,
            now_ms=now,
        )

        self.assertIn("wss://test-machine.tail1234.ts.net/media-stream", ws_url)
        self.assertEqual(validate_media_stream_request(ws_url, secret, now_ms=now), "call-123")

    def test_rejects_expired_or_tampered_urls(self) -> None:
        now = 1_700_000_000_000
        secret = "test-secret"
        ws_url = build_authenticated_media_stream_url(
            TUNNEL_URL,
            "call-123",
            secret,
            now_ms=now,
        )

        self.assertIsNone(validate_media_stream_request(ws_url, secret, now_ms=now + (16 * 60 * 1000)))

        parsed = urlparse(ws_url)
        params = parse_qs(parsed.query)
        params["callId"] = ["call-456"]
        tampered = parsed._replace(
            query="&".join(f"{key}={value[0]}" for key, value in params.items())
        ).geturl()
        self.assertIsNone(validate_media_stream_request(tampered, secret, now_ms=now))


class RateLimitTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_rate_limit_store()

    def test_check_rate_limit_blocks_after_limit(self) -> None:
        request = SimpleNamespace(headers={"X-Forwarded-For": "203.0.113.10"}, remote=None)

        self.assertIsNone(check_rate_limit(request, limit=2, now_ms=1_000))
        self.assertIsNone(check_rate_limit(request, limit=2, now_ms=1_001))
        response = check_rate_limit(request, limit=2, now_ms=1_002)

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.status, 429)
        assert response.text is not None
        self.assertEqual(json.loads(response.text), {"error": "Too many requests"})


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_active_calls(self.db)
        clear_rate_limit_store()
        self.app = create_app(self.db, TUNNEL_URL)
        self.webhooks = create_webhook_handler(self.db, TUNNEL_URL)

    async def asyncTearDown(self) -> None:
        clear_rate_limit_store()
        reset_active_calls(self.db)
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_health_route_returns_ok(self) -> None:
        response = await self._invoke_app("GET", "/health")
        response = cast(web.Response, response)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        payload = json.loads(response.text)
        self.assertEqual(payload["status"], "ok")
        self.assertIsInstance(payload["timestamp"], int)

    async def test_create_app_registers_expected_routes(self) -> None:
        paths = sorted({resource.canonical for resource in self.app.router.resources()})
        for expected in (
            "/health",
            "/media-stream",
            "/webhook/twilio/dial-action",
            "/webhook/twilio/status",
            "/webhook/twilio/voice",
            "/dashboard",
            "/dashboard/login",
            "/dashboard/setup",
            "/dashboard/settings",
            "/dashboard/page/{slug}",
            "/dashboard/stream",
            "/dashboard/api/voice/token",
            "/dashboard/api/voice/disconnect",
            "/static/{name}",
        ):
            self.assertIn(expected, paths)

    async def test_dashboard_page_redirects_to_login_without_session(self) -> None:
        with self.assertRaises(web.HTTPFound) as exc:
            await self._invoke_app("GET", "/dashboard/page/home")

        self.assertIn("/dashboard/login", str(exc.exception.location))

    async def test_dashboard_login_accepts_token_query_and_redirects(self) -> None:
        dashboard = get_providers_config().dashboard
        self.assertIsNotNone(dashboard)
        assert dashboard is not None
        token = dashboard.token
        with self.assertRaises(web.HTTPFound) as exc:
            await self._invoke_app("GET", f"/dashboard/login?token={token}")

        cookies = exc.exception.cookies
        self.assertIn(SESSION_COOKIE, cookies)
        self.assertEqual(cookies[SESSION_COOKIE].value, build_session_cookie(token))
        self.assertEqual(exc.exception.location, "/dashboard/page/home")

    async def test_dashboard_status_fragment_renders_with_session(self) -> None:
        dashboard = get_providers_config().dashboard
        self.assertIsNotNone(dashboard)
        assert dashboard is not None
        response = cast(web.Response, await self._invoke_app(
            "GET",
            "/dashboard/f/status",
            cookies={SESSION_COOKIE: build_session_cookie(dashboard.token)},
        ))

        self.assertEqual(response.status, 200)
        body = response.text or ""
        self.assertIn("Pending actions", body)

    async def test_dashboard_voice_token_returns_room_credentials(self) -> None:
        dashboard = get_providers_config().dashboard
        self.assertIsNotNone(dashboard)
        assert dashboard is not None
        person = upsert_person(self.db, PUBLIC_PHONE, "Owner")
        fake_session = SimpleNamespace(
            call_id="call-browser-1",
            room_name="owner-test-2026-03-29",
            person_id=person.id,
        )

        with (
            patch("mystic.web.acquire_dashboard_voice_session", new=AsyncMock(return_value=fake_session)) as acquire_session,
            patch("mystic.web.generate_token", new=AsyncMock(return_value="test-jwt-token")),
            patch("mystic.web.get_call_transcript", return_value=""),
        ):
            response = cast(web.Response, await self._invoke_app(
                "POST",
                "/dashboard/api/voice/token",
                cookies={SESSION_COOKIE: build_session_cookie(dashboard.token)},
            ))

        self.assertEqual(response.status, 200)
        assert response.text is not None
        body = json.loads(response.text)
        self.assertEqual(body["token"], "test-jwt-token")
        self.assertEqual(body["roomName"], "owner-test-2026-03-29")
        self.assertEqual(body["callId"], "call-browser-1")
        self.assertTrue(body["participantName"].startswith("dashboard-"))
        self.assertIn("url", body)
        acquire_session.assert_awaited_once()
        acquire_session_args = acquire_session.await_args
        self.assertIsNotNone(acquire_session_args)
        assert acquire_session_args is not None
        self.assertTrue(acquire_session_args.kwargs["participant_name"].startswith("dashboard-"))

    async def test_voice_webhook_returns_twiml_and_persists_call(self) -> None:
        params = {"From": PUBLIC_PHONE, "CallSid": "CA-voice-1"}
        signature = _sign_twilio_request("test-twilio-token", f"{TUNNEL_URL}/webhook/twilio/voice", params)
        request = FakeTextRequest(
            body=_form_body(params),
            headers={"X-Twilio-Signature": signature},
        )

        with patch(
            "mystic.calls.create_room",
            new=AsyncMock(return_value="call-room-voice"),
        ):
            response = await self.webhooks.voice(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        body = response.text
        self.assertIn("<Stream", body)
        self.assertIn("media-stream", body)
        self.assertIn("token=", body)
        call = get_call_by_external_id(self.db, "CA-voice-1")
        self.assertIsNotNone(call)
        self.assertEqual(get_active_call_count(self.db), 1)

    async def test_status_webhook_rejects_invalid_signature(self) -> None:
        response = await self.webhooks.status(
            FakeTextRequest(
                body=_form_body({"CallSid": "CA-invalid-1", "CallStatus": "completed"}),
                headers={"X-Twilio-Signature": "not-valid"},
            )
        )

        self.assertEqual(response.status, 401)

    async def test_status_webhook_marks_no_answered_calls_as_unanswered(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Caller")
        insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-no-answer-1",
        )
        params = {"CallSid": "CA-no-answer-1", "CallStatus": "no-answer"}
        signature = _sign_twilio_request("test-twilio-token", f"{TUNNEL_URL}/webhook/twilio/status", params)

        response = await self.webhooks.status(
            FakeTextRequest(
                body=_form_body(params),
                headers={"X-Twilio-Signature": signature},
            )
        )

        self.assertEqual(response.status, 200)
        updated = get_call_by_external_id(self.db, "CA-no-answer-1")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNotNone(updated.ended_at)

    async def test_status_webhook_marks_answered_calls(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Caller")
        insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-answered-1",
        )
        params = {"CallSid": "CA-answered-1", "CallStatus": "answered"}
        signature = _sign_twilio_request("test-twilio-token", f"{TUNNEL_URL}/webhook/twilio/status", params)

        response = await self.webhooks.status(
            FakeTextRequest(
                body=_form_body(params),
                headers={"X-Twilio-Signature": signature},
            )
        )

        self.assertEqual(response.status, 200)
        updated = get_call_by_external_id(self.db, "CA-answered-1")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNotNone(updated.answered_at)
        self.assertIsNone(updated.ended_at)

    async def test_status_webhook_marks_completed_calls(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Caller")
        insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-completed-1",
        )
        params = {
            "CallSid": "CA-completed-1",
            "CallStatus": "completed",
            "CallDuration": "63",
        }
        signature = _sign_twilio_request("test-twilio-token", f"{TUNNEL_URL}/webhook/twilio/status", params)

        response = await self.webhooks.status(
            FakeTextRequest(
                body=_form_body(params),
                headers={"X-Twilio-Signature": signature},
            )
        )

        self.assertEqual(response.status, 200)
        updated = get_call_by_external_id(self.db, "CA-completed-1")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNotNone(updated.ended_at)
        self.assertEqual(updated.duration, 63)

    async def test_status_webhook_reconnects_caller_on_warm_transfer_failure(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Caller")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="public",
            external_id="CA-caller-transfer-1",
        )
        params = {"CallSid": "CA-target-transfer-1", "CallStatus": "no-answer"}
        callback_url = f"{TUNNEL_URL}/webhook/twilio/status?callerCallId={call.id}"
        signature = _sign_twilio_request("test-twilio-token", callback_url, params)

        with (
            patch("mystic.calls.reconnect_call_to_stream", new=AsyncMock(return_value="<Response />")) as reconnect,
            patch("mystic.calls.update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            response = await self.webhooks.status(
                FakeTextRequest(
                    body=_form_body(params),
                    headers={"X-Twilio-Signature": signature},
                    query={"callerCallId": call.id},
                )
            )

        self.assertEqual(response.status, 200)
        reconnect.assert_awaited_once_with(self.db, call.id, TUNNEL_URL)
        update_live_call_mock.assert_awaited_once_with(
            get_providers_config().twilio,
            "CA-caller-transfer-1",
            twiml="<Response />",
        )

    async def test_media_stream_route_rejects_invalid_signature(self) -> None:
        response = await self._invoke_app(
            "GET",
            "/media-stream?callId=call-123&expiresAt=9999999999999&token=bad",
        )

        self.assertEqual(response.status, 401)

    async def test_media_stream_route_uses_audio_bridge(self) -> None:
        providers = get_providers_config()
        assert providers.twilio is not None
        signed_url = build_authenticated_media_stream_url(
            TUNNEL_URL,
            "call-123",
            providers.twilio.authToken,
            now_ms=int(time.time() * 1000),
        )
        path = urlparse(signed_url).path + "?" + urlparse(signed_url).query
        events: list[str] = []

        fake_ws = FakeWebSocketResponse(
            [SimpleNamespace(type=WSMsgType.TEXT, data='{"event":"connected"}')]
        )

        class FakeBridge:
            async def start(self) -> None:
                events.append("start")

            async def handle_twilio_message(self, raw_message: str) -> None:
                events.append(f"message:{raw_message}")

            async def stop(self) -> None:
                events.append("stop")

        with (
            patch("mystic.server.create_audio_bridge", return_value=FakeBridge()),
            patch("mystic.server.web.WebSocketResponse", return_value=fake_ws),
        ):
            response = await self._invoke_app("GET", path)
            await asyncio.sleep(0)

        self.assertIs(response, fake_ws)
        self.assertEqual(events[0], "start")
        self.assertIn('message:{"event":"connected"}', events)
        self.assertEqual(events[-1], "stop")

    async def _invoke_app(self, method: str, path: str, *, cookies: dict[str, str] | None = None):
        headers = {"Host": "localhost"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        request = make_mocked_request(
            method,
            path,
            headers=headers,
            app=self.app,
        )
        match_info = await self.app.router.resolve(request)
        return await match_info.handler(request)


class FakeTextRequest:
    def __init__(
        self,
        *,
        body: str,
        headers: dict[str, str],
        query: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self._headers = headers
        self.query = query or {}

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    async def text(self) -> str:
        return self.body


class FakeWebSocketResponse:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self._messages = list(messages)
        self.closed = False
        self.prepared = False
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []

    async def prepare(self, _request: object) -> None:
        self.prepared = True

    def exception(self) -> None:
        return None

    async def send_str(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_json(self, payload: object) -> None:
        self.sent_text.append(json.dumps(payload))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    def __aiter__(self) -> "FakeWebSocketResponse":
        return self

    async def __anext__(self) -> SimpleNamespace:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self, code: int = 1000, message: bytes = b"") -> None:
        del code, message
        self.closed = True


def _sign_twilio_request(auth_token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _form_body(params: dict[str, str]) -> str:
    return urlencode(params)


if __name__ == "__main__":
    unittest.main()
