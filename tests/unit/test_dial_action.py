"""Tests for the /webhook/twilio/dial-action endpoint."""

from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from mystic.db import close_database, initialize_schema, insert_call, open_database, upsert_person
from mystic.server import WebhookHandler
from tests.python_helpers import TempAppHome, seed_core_files

TUNNEL_URL = "https://test.ts.net"
AUTH_TOKEN = "test-twilio-token"


def _twilio_signature(url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(AUTH_TOKEN.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _make_request(
    params: dict[str, str],
    *,
    call_id: str = "",
    reconnect: bool = False,
    signature: str | None = None,
) -> SimpleNamespace:
    query_params: list[tuple[str, str]] = []
    if call_id:
        query_params.append(("callId", call_id))
    if reconnect:
        query_params.append(("reconnect", "1"))
    query_string = urlencode(query_params)
    base_path = "/webhook/twilio/dial-action"
    url = f"{TUNNEL_URL}{base_path}?{query_string}" if query_string else f"{TUNNEL_URL}{base_path}"
    body = urlencode(params)

    if signature is None:
        signature = _twilio_signature(url, params)

    return SimpleNamespace(
        text=AsyncMock(return_value=body),
        headers={"X-Twilio-Signature": signature},
        query={
            key: value
            for key, value in (
                ("callId", call_id),
                ("reconnect", "1" if reconnect else ""),
            )
            if value
        },
    )


class DialActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        self.handler = WebhookHandler(db=self.db, tunnel_url=TUNNEL_URL)

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
            external_id="CA-test-001",
        )

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_completed_status_returns_hangup(self) -> None:
        params = {"DialCallStatus": "completed", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id)

        response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("<Hangup/>", response.text)
        self.assertNotIn("<Connect>", response.text)

    async def test_completed_status_with_reconnect_returns_stream(self) -> None:
        params = {"DialCallStatus": "completed", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id, reconnect=True)

        stream_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Connect><Stream>test</Stream></Connect></Response>"
        )
        with patch(
            "mystic.calls.reconnect_call_to_stream",
            new=AsyncMock(return_value=stream_twiml),
        ) as mock_reconnect:
            response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("<Say>Let me reconnect you.</Say>", response.text)
        self.assertIn("<Connect>", response.text)
        self.assertNotIn("<Hangup/>", response.text)
        mock_reconnect.assert_awaited_once_with(self.db, self.call.id, TUNNEL_URL)

    async def test_no_answer_reconnects_with_say_and_stream(self) -> None:
        params = {"DialCallStatus": "no-answer", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id)

        stream_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Connect><Stream>test</Stream></Connect></Response>"
        )
        with patch(
            "mystic.calls.reconnect_call_to_stream",
            new=AsyncMock(return_value=stream_twiml),
        ) as mock_reconnect:
            response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("<Say>", response.text)
        self.assertIn("didn't answer", response.text)
        self.assertIn("<Connect>", response.text)
        mock_reconnect.assert_awaited_once_with(self.db, self.call.id, TUNNEL_URL)

    async def test_busy_reconnects(self) -> None:
        params = {"DialCallStatus": "busy", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id)

        stream_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Connect><Stream>test</Stream></Connect></Response>"
        )
        with patch(
            "mystic.calls.reconnect_call_to_stream",
            new=AsyncMock(return_value=stream_twiml),
        ):
            response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("<Say>", response.text)
        self.assertIn("<Connect>", response.text)

    async def test_missing_call_id_returns_error_twiml(self) -> None:
        params = {"DialCallStatus": "no-answer", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id="")

        response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Something went wrong", response.text)
        self.assertIn("<Hangup/>", response.text)

    async def test_reconnect_failure_returns_error_twiml(self) -> None:
        params = {"DialCallStatus": "no-answer", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id)

        with patch(
            "mystic.calls.reconnect_call_to_stream",
            new=AsyncMock(return_value=None),
        ):
            response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 200)
        assert response.text is not None
        self.assertIn("Something went wrong", response.text)
        self.assertIn("<Hangup/>", response.text)

    async def test_invalid_signature_returns_401(self) -> None:
        params = {"DialCallStatus": "completed", "CallSid": "CA-test-001"}
        request = _make_request(params, call_id=self.call.id, signature="bad-signature")

        response = await self.handler.dial_action(request)

        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
