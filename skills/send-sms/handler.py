"""Operational handler for send-sms."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.calls import send_sms
from mystic.config import get_error_message, get_twilio_config, is_valid_e164
from mystic.db import get_person_by_id
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    config = get_twilio_config()
    if config is None:
        return "Twilio is not configured. Run init --connect-twilio first."

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return "Please provide a message to send."

    phone = params.get("phone")
    if isinstance(phone, str) and phone:
        if not is_valid_e164(phone):
            return f"Invalid phone number: {phone}. Use E.164 format (+15551234567)."
    else:
        person = get_person_by_id(db, ctx.person_id)
        if person is None or not person.phone:
            return "No phone number provided and none on file for this person."
        phone = person.phone

    try:
        sid = await send_sms(config, phone, message.strip())
    except Exception as exc:
        return f"Failed to send SMS: {get_error_message(exc)}"

    return f"SMS sent to {phone} (id: {sid[:8]})"
