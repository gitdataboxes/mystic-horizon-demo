from __future__ import annotations

from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from mystic.db import get_call_by_external_id, insert_call, upsert_person
from mystic.server import create_webhook_handler
from tests.integration.helpers import PUBLIC_PHONE, TUNNEL_URL, sign_twilio_request


async def test_voice_webhook_accepts_valid_signature_and_persists_call(
    integration_env,
) -> None:
    webhooks = create_webhook_handler(integration_env.db, integration_env.tunnel_url)
    params = {"From": PUBLIC_PHONE, "CallSid": "CA-voice-integration-1"}
    signature = sign_twilio_request(
        "test-twilio-token",
        f"{TUNNEL_URL}/webhook/twilio/voice",
        params,
    )

    with patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-webhook")):
        response = await webhooks.voice(
            _FakeTextRequest(
                body=urlencode(params),
                headers={"X-Twilio-Signature": signature},
            )
        )
        body = response.text

    assert response.status == 200
    assert body is not None
    assert "<Stream" in body
    call = get_call_by_external_id(integration_env.db, "CA-voice-integration-1")
    assert call is not None
    assert call.audience == "public"


async def test_status_webhook_rejects_invalid_signature_and_accepts_completed(
    integration_env,
) -> None:
    webhooks = create_webhook_handler(integration_env.db, integration_env.tunnel_url)
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Webhook Caller")
    insert_call(
        integration_env.db,
        person_id=person.id,
        direction="outbound",
        audience="public",
        external_id="CA-status-integration-1",
    )

    invalid = await webhooks.status(
        _FakeTextRequest(
            body=urlencode({"CallSid": "CA-status-integration-1", "CallStatus": "completed"}),
            headers={"X-Twilio-Signature": "bad-signature"},
        )
    )
    assert invalid.status == 401

    params = {
        "CallSid": "CA-status-integration-1",
        "CallStatus": "completed",
        "CallDuration": "63",
    }
    valid_signature = sign_twilio_request(
        "test-twilio-token",
        f"{TUNNEL_URL}/webhook/twilio/status",
        params,
    )
    valid = await webhooks.status(
        _FakeTextRequest(
            body=urlencode(params),
            headers={"X-Twilio-Signature": valid_signature},
        )
    )
    payload = valid.text

    assert valid.status == 200
    assert payload == '{"ok": true}'
    updated = get_call_by_external_id(integration_env.db, "CA-status-integration-1")
    assert updated is not None
    assert updated.ended_at is not None
    assert updated.duration == 63


class _FakeTextRequest:
    def __init__(self, *, body: str, headers: dict[str, str]) -> None:
        self._body = body
        self._headers = headers

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    async def text(self) -> str:
        return self._body
