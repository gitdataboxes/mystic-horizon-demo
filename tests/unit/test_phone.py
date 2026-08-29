from __future__ import annotations

import unittest
from collections.abc import Mapping
from unittest.mock import Mock, patch

from mystic.config import TwilioConfig
from mystic.http import HttpResponse
from mystic.phone import ensure_phone_line_ready

CONFIG = TwilioConfig(
    accountSid="AC123",
    authToken="secret",
    phoneNumber="+15550001111",
    phoneNumberSid="PN123",
)


class PhoneReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_when_funnel_and_twilio_webhooks_match(self) -> None:
        requests: list[tuple[str, str]] = []

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del headers, payload, timeout
            requests.append((method, url))
            return HttpResponse(
                status_code=200,
                content=(
                    b'{"sid":"PN123","phone_number":"+15550001111",'
                    b'"voice_url":"https://agent.tail1234.ts.net/webhook/twilio/voice",'
                    b'"status_callback":"https://agent.tail1234.ts.net/webhook/twilio/status"}'
                ),
            )

        with (
            patch("mystic.phone.get_providers_config", return_value=Mock(twilio=None)),
            patch("mystic.phone.check_tailscale_ready", return_value=(True, "")),
            patch("mystic.phone.get_tailscale_hostname", return_value="agent.tail1234.ts.net"),
            patch(
                "mystic.phone.get_tailscale_funnel_status",
                return_value=(True, "https://agent.tail1234.ts.net\n|-- proxy http://127.0.0.1:3000"),
            ),
        ):
            readiness = await ensure_phone_line_ready(
                port=3000,
                twilio_config=CONFIG,
                repair=True,
                twilio_transport=transport,
            )

        self.assertEqual(readiness.status, "ok")
        self.assertEqual(readiness.repaired, [])
        self.assertEqual(requests, [("GET", "https://api.twilio.com/2010-04-01/Accounts/AC123/IncomingPhoneNumbers/PN123.json")])

    async def test_patches_twilio_only_when_webhooks_mismatch(self) -> None:
        methods: list[str] = []

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del url, headers, timeout
            methods.append(method)
            if method == "GET":
                return HttpResponse(
                    status_code=200,
                    content=(
                        b'{"sid":"PN123","phone_number":"+15550001111",'
                        b'"voice_url":"https://old.example/voice",'
                        b'"status_callback":"https://old.example/status"}'
                    ),
                )
            assert payload is not None
            self.assertIn(b"VoiceUrl=https%3A%2F%2Fagent.tail1234.ts.net%2Fwebhook%2Ftwilio%2Fvoice", payload)
            return HttpResponse(status_code=200, content=b"{}")

        with (
            patch("mystic.phone.get_providers_config", return_value=Mock(twilio=None)),
            patch("mystic.phone.check_tailscale_ready", return_value=(True, "")),
            patch("mystic.phone.get_tailscale_hostname", return_value="agent.tail1234.ts.net"),
            patch(
                "mystic.phone.get_tailscale_funnel_status",
                return_value=(True, "https://agent.tail1234.ts.net\n|-- proxy http://127.0.0.1:3000"),
            ),
        ):
            readiness = await ensure_phone_line_ready(
                port=3000,
                twilio_config=CONFIG,
                repair=True,
                twilio_transport=transport,
            )

        self.assertEqual(readiness.status, "ok")
        self.assertEqual(methods, ["GET", "POST"])
        self.assertEqual(readiness.repaired, ["twilio_webhooks"])

    async def test_inspect_mode_reports_mismatch_without_patching(self) -> None:
        methods: list[str] = []

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del url, headers, payload, timeout
            methods.append(method)
            return HttpResponse(
                status_code=200,
                content=(
                    b'{"sid":"PN123","phone_number":"+15550001111",'
                    b'"voice_url":"https://old.example/voice",'
                    b'"status_callback":"https://old.example/status"}'
                ),
            )

        with (
            patch("mystic.phone.get_providers_config", return_value=Mock(twilio=None)),
            patch("mystic.phone.check_tailscale_ready", return_value=(True, "")),
            patch("mystic.phone.get_tailscale_hostname", return_value="agent.tail1234.ts.net"),
            patch(
                "mystic.phone.get_tailscale_funnel_status",
                return_value=(True, "https://agent.tail1234.ts.net\n|-- proxy http://127.0.0.1:3000"),
            ),
        ):
            readiness = await ensure_phone_line_ready(
                port=3000,
                twilio_config=CONFIG,
                repair=False,
                twilio_transport=transport,
            )

        self.assertEqual(readiness.status, "degraded")
        self.assertEqual(methods, ["GET"])
        self.assertIn("webhooks do not match", readiness.reason())

    async def test_tailscale_down_marks_phone_offline_without_twilio_call(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            raise AssertionError("Twilio should not be called when Tailscale is offline")

        with (
            patch("mystic.phone.get_providers_config", return_value=Mock(twilio=None)),
            patch("mystic.phone.check_tailscale_ready", return_value=(False, "daemon not running")),
        ):
            readiness = await ensure_phone_line_ready(
                port=3000,
                twilio_config=CONFIG,
                repair=True,
                twilio_transport=transport,
            )

        self.assertEqual(readiness.status, "offline")
        self.assertIn("daemon not running", readiness.reason())


if __name__ == "__main__":
    unittest.main()

