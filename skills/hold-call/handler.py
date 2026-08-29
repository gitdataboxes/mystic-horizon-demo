"""Operational handler for hold-call."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.calls import generate_hold_twiml, update_live_call
from mystic.config import get_error_message, get_tunnel_url, get_twilio_config
from mystic.db import get_call_by_id
from mystic.server import build_authenticated_media_stream_url
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    config = get_twilio_config()
    if config is None:
        return "Twilio is not configured. Run init --connect-twilio first."

    call = get_call_by_id(db, ctx.call_id)
    if call is None or not call.external_id:
        return "This call cannot be placed on hold (local-only)."

    tunnel_url = get_tunnel_url()
    if not tunnel_url:
        return "This call cannot be placed on hold right now."

    hold_message = params.get("hold_message")
    message = hold_message.strip() if isinstance(hold_message, str) and hold_message.strip() else "Please hold."
    ws_url = build_authenticated_media_stream_url(tunnel_url, ctx.call_id, config.authToken)
    twiml = generate_hold_twiml(
        message,
        resume_ws_url=ws_url,
        resume_params={"callId": ctx.call_id},
    )

    try:
        await update_live_call(config, call.external_id, twiml=twiml)
    except Exception as exc:
        return f"Failed to place caller on hold: {get_error_message(exc)}"

    return "Caller is on hold."
