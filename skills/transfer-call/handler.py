"""Operational handler for transfer-call."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.calls import generate_dial_twiml, update_live_call
from mystic.config import (
    get_agent_config,
    get_error_message,
    get_tunnel_url,
    get_twilio_config,
    is_valid_e164,
)
from mystic.db import get_call_by_id
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    config = get_twilio_config()
    if config is None:
        return "Twilio is not configured. Run init --connect-twilio first."

    destination = params.get("destination")
    if not isinstance(destination, str) or not destination.strip():
        return "Please provide a transfer destination."

    raw_destination = destination.strip()
    normalized_destination = raw_destination.lower()

    if ctx.audience == "public" and normalized_destination != "owner":
        return "Public callers can only transfer to the owner."

    resolved_destination = raw_destination
    if normalized_destination == "owner":
        owner_phone = get_agent_config().owner.phone
        if not owner_phone:
            return "Owner phone is not configured."
        resolved_destination = owner_phone

    if not is_valid_e164(resolved_destination):
        return (
            f"Invalid transfer destination: {resolved_destination}. "
            "Use E.164 format (+15551234567)."
        )

    call = get_call_by_id(db, ctx.call_id)
    if call is None or not call.external_id:
        return "This call cannot be transferred (local-only)."

    tunnel_url = get_tunnel_url()
    action_url = f"{tunnel_url}/webhook/twilio/dial-action?callId={ctx.call_id}" if tunnel_url else None
    twiml = generate_dial_twiml(resolved_destination, caller_id=config.phoneNumber, action=action_url)

    try:
        await update_live_call(config, call.external_id, twiml=twiml)
    except Exception as exc:
        return f"Failed to transfer call: {get_error_message(exc)}"

    return f"Transferring call to {resolved_destination}."
