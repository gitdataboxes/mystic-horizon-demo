from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

from mystic.calls import (
    buy_phone_number,
    generate_conference_twiml,
    generate_dial_twiml,
    generate_hold_twiml,
    generate_say_twiml,
    generate_say_conference_twiml,
    generate_stream_twiml,
    get_incoming_phone_number,
    make_outbound_call,
    search_available_numbers,
    update_live_call,
    update_phone_webhook,
    validate_twilio_signature,
)
from mystic.config import TwilioConfig
from mystic.http import HttpResponse

CONFIG = TwilioConfig(
    accountSid="AC_test",
    authToken="auth_test",
    phoneNumber="+15551230000",
)


class TwimlTests(unittest.TestCase):
    def test_generate_dial_twiml_defaults_to_30_second_timeout(self) -> None:
        xml = generate_dial_twiml("+15551234567")
        self.assertEqual(
            xml,
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Dial timeout="30">+15551234567</Dial></Response>',
        )

    def test_generate_dial_twiml_includes_caller_id(self) -> None:
        xml = generate_dial_twiml("+15551234567", caller_id="+15559999999")
        self.assertIn('callerId="+15559999999"', xml)

    def test_generate_dial_twiml_respects_custom_timeout(self) -> None:
        xml = generate_dial_twiml("+15551234567", timeout=45)
        self.assertIn('timeout="45"', xml)

    def test_generate_dial_twiml_includes_action_url(self) -> None:
        xml = generate_dial_twiml("+15551234567", action="https://example.test/dial-action?callId=abc")
        self.assertIn('action="https://example.test/dial-action?callId=abc"', xml)

    def test_generate_dial_twiml_escapes_action_url(self) -> None:
        xml = generate_dial_twiml("+15551234567", action='https://example.test/a?b="c"&d=e')
        self.assertIn("&quot;c&quot;", xml)
        self.assertIn("&amp;d=e", xml)

    def test_generate_dial_twiml_omits_action_when_none(self) -> None:
        xml = generate_dial_twiml("+15551234567")
        self.assertNotIn("action=", xml)

    def test_generate_dial_twiml_escapes_destination(self) -> None:
        xml = generate_dial_twiml('+1555<123>&"')
        self.assertIn("+1555&lt;123&gt;&amp;&quot;", xml)

    def test_generate_hold_twiml_defaults_to_auto_resume_loop(self) -> None:
        xml = generate_hold_twiml(resume_ws_url="wss://example.test/stream")
        self.assertIn('<Say loop="10">Please hold.</Say>', xml)
        self.assertIn('<Stream url="wss://example.test/stream">', xml)

    def test_generate_hold_twiml_supports_custom_message(self) -> None:
        xml = generate_hold_twiml("One moment please.", resume_ws_url="wss://example.test/stream")
        self.assertIn("One moment please.", xml)

    def test_generate_hold_twiml_includes_resume_params(self) -> None:
        xml = generate_hold_twiml(
            resume_ws_url="wss://example.test/stream",
            resume_params={"callId": "call-123"},
        )
        self.assertIn('<Parameter name="callId" value="call-123" />', xml)

    def test_generate_hold_twiml_without_resume_url_loops_forever(self) -> None:
        xml = generate_hold_twiml()
        self.assertIn('<Say loop="0">Please hold.</Say>', xml)
        self.assertNotIn("<Connect>", xml)

    def test_generate_conference_twiml_builds_dial_conference(self) -> None:
        xml = generate_conference_twiml("transfer-call-123")
        self.assertEqual(
            xml,
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Dial><Conference startConferenceOnEnter="true" '
            'endConferenceOnExit="false" beep="false">transfer-call-123</Conference></Dial></Response>',
        )

    def test_generate_conference_twiml_includes_action_url(self) -> None:
        xml = generate_conference_twiml(
            "transfer-call-123",
            action="https://example.test/dial-action?callId=abc&reconnect=1",
        )
        self.assertIn('action="https://example.test/dial-action?callId=abc&amp;reconnect=1"', xml)

    def test_generate_say_conference_twiml_chains_say_then_conference(self) -> None:
        xml = generate_say_conference_twiml(
            "Connecting your call.",
            "transfer-call-123",
            end_on_exit=True,
            beep=True,
        )
        self.assertIn("<Say>Connecting your call.</Say>", xml)
        self.assertIn('<Dial><Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="true">', xml)

    def test_generate_stream_twiml_escapes_attribute_values(self) -> None:
        xml = generate_stream_twiml(
            'wss://example.test/media?voice="agent"&mode=call',
            {
                "callId": "abc<123>",
                "note": 'O\'Reilly & "friends"',
            },
        )

        self.assertIn("&quot;agent&quot;", xml)
        self.assertIn("abc&lt;123&gt;", xml)
        self.assertIn("O&apos;Reilly &amp; &quot;friends&quot;", xml)

    def test_generate_say_twiml_escapes_message(self) -> None:
        xml = generate_say_twiml('Use < & > "quotes"')
        self.assertIn("&lt;", xml)
        self.assertIn("&amp;", xml)
        self.assertIn("&quot;quotes&quot;", xml)


class TwilioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_available_numbers_builds_query_and_parses_results(self) -> None:
        captured: dict[str, object] = {}

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            captured.update(
                method=method,
                url=url,
                headers=dict(headers),
                payload=payload,
                timeout=timeout,
            )
            return HttpResponse(
                status_code=200,
                content=(
                    b'{"available_phone_numbers":'
                    b'[{"phone_number":"+14155550123","friendly_name":"(415) 555-0123"}]}'
                ),
                headers={"content-type": "application/json"},
            )

        result = await search_available_numbers(CONFIG, area_code="415", transport=transport)

        self.assertEqual(
            result,
            [{"phoneNumber": "+14155550123", "friendlyName": "(415) 555-0123"}],
        )
        self.assertEqual(captured["method"], "GET")
        parsed = urlparse(str(captured["url"]))
        self.assertEqual(parse_qs(parsed.query)["PageSize"], ["5"])
        self.assertEqual(parse_qs(parsed.query)["AreaCode"], ["415"])
        self.assertIsNone(captured["payload"])
        self.assertIn("Basic ", str(captured["headers"]))

    async def test_search_available_numbers_raises_on_http_error(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, headers, payload, timeout
            return HttpResponse(status_code=500, content=b"bad")

        with self.assertRaisesRegex(RuntimeError, "Twilio search failed: 500 bad"):
            await search_available_numbers(CONFIG, transport=transport)

    async def test_buy_phone_number_posts_form_data(self) -> None:
        captured_payload: bytes | None = None

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del url, headers, timeout
            nonlocal captured_payload
            self.assertEqual(method, "POST")
            captured_payload = payload
            return HttpResponse(
                status_code=200,
                content=b'{"sid":"PN123","phone_number":"+14155550123"}',
            )

        result = await buy_phone_number(
            CONFIG,
            "+14155550123",
            "https://example.test/voice",
            "https://example.test/status",
            transport=transport,
        )

        assert captured_payload is not None
        params = parse_qs(captured_payload.decode("utf-8"))
        self.assertEqual(result, {"sid": "PN123", "phoneNumber": "+14155550123"})
        self.assertEqual(params["PhoneNumber"], ["+14155550123"])
        self.assertEqual(params["VoiceUrl"], ["https://example.test/voice"])
        self.assertEqual(params["StatusCallback"], ["https://example.test/status"])

    async def test_update_phone_webhook_posts_urls(self) -> None:
        captured_payload: bytes | None = None

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del headers, timeout
            nonlocal captured_payload
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/IncomingPhoneNumbers/PN123.json"))
            captured_payload = payload
            return HttpResponse(status_code=200, content=b"{}")

        await update_phone_webhook(
            CONFIG,
            "PN123",
            "https://example.test/voice",
            "https://example.test/status",
            transport=transport,
        )

        assert captured_payload is not None
        params = parse_qs(captured_payload.decode("utf-8"))
        self.assertEqual(params["VoiceUrl"], ["https://example.test/voice"])
        self.assertEqual(params["StatusCallback"], ["https://example.test/status"])

    async def test_get_incoming_phone_number_parses_webhook_urls(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del headers, timeout
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/IncomingPhoneNumbers/PN123.json"))
            self.assertIsNone(payload)
            return HttpResponse(
                status_code=200,
                content=(
                    b'{"sid":"PN123","phone_number":"+14155550123","friendly_name":"Line",'
                    b'"voice_url":"https://example.test/voice",'
                    b'"status_callback":"https://example.test/status"}'
                ),
            )

        number = await get_incoming_phone_number(CONFIG, "PN123", transport=transport)

        self.assertEqual(number["phoneNumber"], "+14155550123")
        self.assertEqual(number["voiceUrl"], "https://example.test/voice")
        self.assertEqual(number["statusCallback"], "https://example.test/status")

    async def test_make_outbound_call_repeats_status_callback_events(self) -> None:
        captured_payload: bytes | None = None

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, headers, timeout
            nonlocal captured_payload
            captured_payload = payload
            return HttpResponse(status_code=200, content=b'{"sid":"CA123"}')

        sid = await make_outbound_call(
            CONFIG,
            "+15559876543",
            "<Response />",
            "https://example.test/status",
            transport=transport,
        )

        self.assertEqual(sid, "CA123")
        assert captured_payload is not None
        params = parse_qs(captured_payload.decode("utf-8"))
        self.assertEqual(
            params["StatusCallbackEvent"],
            ["initiated", "ringing", "answered", "completed"],
        )

    async def test_make_outbound_call_requires_sid(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, headers, payload, timeout
            return HttpResponse(status_code=200, content=b"{}")

        with self.assertRaisesRegex(RuntimeError, "did not include a call sid"):
            await make_outbound_call(
                CONFIG,
                "+15559876543",
                "<Response />",
                "https://example.test/status",
                transport=transport,
            )

    async def test_update_live_call_posts_twiml(self) -> None:
        captured_url = ""
        captured_payload: bytes | None = None

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del headers, timeout
            nonlocal captured_url, captured_payload
            self.assertEqual(method, "POST")
            captured_url = url
            captured_payload = payload
            return HttpResponse(status_code=200, content=b"{}")

        await update_live_call(
            CONFIG,
            "CA123",
            twiml="<Response><Dial>+15551234567</Dial></Response>",
            transport=transport,
        )

        self.assertTrue(captured_url.endswith("/Calls/CA123.json"))
        assert captured_payload is not None
        params = parse_qs(captured_payload.decode("utf-8"))
        self.assertEqual(params["Twiml"], ["<Response><Dial>+15551234567</Dial></Response>"])
        self.assertNotIn("Url", params)

    async def test_update_live_call_posts_url(self) -> None:
        captured_payload: bytes | None = None

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, headers, timeout
            nonlocal captured_payload
            captured_payload = payload
            return HttpResponse(status_code=200, content=b"{}")

        await update_live_call(
            CONFIG,
            "CA123",
            url="https://example.test/twiml",
            transport=transport,
        )

        assert captured_payload is not None
        params = parse_qs(captured_payload.decode("utf-8"))
        self.assertEqual(params["Url"], ["https://example.test/twiml"])
        self.assertNotIn("Twiml", params)

    async def test_update_live_call_requires_twiml_or_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires twiml or url"):
            await update_live_call(CONFIG, "CA123")


class TwilioSignatureTests(unittest.TestCase):
    def test_validate_twilio_signature_accepts_matching_signature(self) -> None:
        url = "https://example.test/webhook/twilio/status"
        params = {"CallSid": "CA123", "CallStatus": "completed"}
        signature = _twilio_signature(CONFIG.authToken, url, params)

        self.assertTrue(validate_twilio_signature(CONFIG, signature, url, params))

    def test_validate_twilio_signature_rejects_wrong_signature(self) -> None:
        url = "https://example.test/webhook/twilio/status"
        params = {"CallSid": "CA123", "CallStatus": "completed"}
        self.assertFalse(validate_twilio_signature(CONFIG, "bad-signature", url, params))


def _twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


if __name__ == "__main__":
    unittest.main()
