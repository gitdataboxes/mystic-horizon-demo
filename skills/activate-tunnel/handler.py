"""Operational handler for activate-tunnel."""

from __future__ import annotations

from typing import Mapping

from mystic.config import (
    get_agent_config,
    get_providers_config,
)
from mystic.phone import ensure_phone_line_ready
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    _params: Mapping[str, object],
) -> str:
    providers = get_providers_config()
    twilio_config = providers.twilio
    if twilio_config is None:
        return "Twilio is not configured. Save credentials and a phone number first."

    if not twilio_config.phoneNumberSid:
        return "Twilio phone number SID is missing. Attach or buy a number with write-twilio-number first."

    readiness = await ensure_phone_line_ready(port=get_agent_config().server.port, repair=True)
    if readiness.status == "ok":
        webhook_text = "Twilio webhooks patched" if "twilio_webhooks" in readiness.repaired else "Twilio webhooks verified"
        return f"Tunnel active at {readiness.public_url}. {webhook_text}. Phone line is live."
    if readiness.public_url:
        return f"Tunnel active at {readiness.public_url}, but phone line is {readiness.status}: {readiness.reason()}"
    return f"Phone line is {readiness.status}: {readiness.reason()}"
