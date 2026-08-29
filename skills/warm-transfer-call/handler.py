"""Operational handler for warm-transfer-call."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.calls import (
    clear_transfer_target_sid,
    end_call,
    generate_conference_twiml,
    generate_hold_twiml,
    generate_say_conference_twiml,
    make_outbound_call,
    set_transfer_target_sid,
    update_live_call,
)
from mystic.config import (
    get_agent_config,
    get_error_message,
    get_tunnel_url,
    get_twilio_config,
    is_valid_e164,
)
from mystic.db import get_call_by_id
from mystic.server import build_authenticated_media_stream_url
from mystic.types import OperationalContext

_DEFAULT_INTRODUCTION = "You have a call being transferred to you."
_HOLD_MESSAGE = "Please hold while I connect your call."


def _resolve_destination(ctx: OperationalContext, raw_destination: str) -> str | None:
    normalized_destination = raw_destination.lower()
    if normalized_destination == "owner":
        owner_phone = get_agent_config().owner.phone
        return owner_phone if owner_phone else None
    if ctx.audience != "owner":
        return None
    return raw_destination


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    if ctx.audience != "owner":
        return "Only the owner can warm-transfer calls."

    config = get_twilio_config()
    if config is None:
        return "Twilio is not configured. Run init --connect-twilio first."

    destination = params.get("destination")
    if not isinstance(destination, str) or not destination.strip():
        return "Please provide a transfer destination."

    call = get_call_by_id(db, ctx.call_id)
    if call is None or not call.external_id:
        return "This call cannot be warm-transferred (local-only)."

    tunnel_url = get_tunnel_url()
    if not tunnel_url:
        return "Warm transfer is not available right now."

    raw_destination = destination.strip()
    resolved_destination = _resolve_destination(ctx, raw_destination)
    if raw_destination.lower() == "owner" and resolved_destination is None:
        return "Owner phone is not configured."
    if resolved_destination is None or not is_valid_e164(resolved_destination):
        return (
            f"Invalid transfer destination: {raw_destination}. "
            "Use E.164 format (+15551234567) or 'owner'."
        )

    introduction = params.get("introduction")
    intro_message = (
        introduction.strip()
        if isinstance(introduction, str) and introduction.strip()
        else _DEFAULT_INTRODUCTION
    )

    conference_name = f"transfer-{ctx.call_id}"
    resume_ws_url = build_authenticated_media_stream_url(tunnel_url, ctx.call_id, config.authToken)
    hold_twiml = generate_hold_twiml(
        _HOLD_MESSAGE,
        resume_ws_url=resume_ws_url,
        resume_params={"callId": ctx.call_id},
    )

    try:
        await update_live_call(config, call.external_id, twiml=hold_twiml)
    except Exception as exc:
        return f"Failed to warm-transfer call: {get_error_message(exc)}"

    target_sid: str | None = None
    try:
        target_twiml = generate_say_conference_twiml(
            intro_message,
            conference_name,
            end_on_exit=True,
        )
        status_callback = f"{tunnel_url}/webhook/twilio/status?callerCallId={ctx.call_id}"
        target_sid = await make_outbound_call(
            config,
            resolved_destination,
            target_twiml,
            status_callback=status_callback,
        )
        set_transfer_target_sid(ctx.call_id, target_sid)

        reconnect_action_url = (
            f"{tunnel_url}/webhook/twilio/dial-action?callId={ctx.call_id}&reconnect=1"
        )
        caller_conference_twiml = generate_conference_twiml(
            conference_name,
            action=reconnect_action_url,
        )
        await update_live_call(config, call.external_id, twiml=caller_conference_twiml)
    except Exception as exc:
        clear_transfer_target_sid(ctx.call_id)
        if target_sid is not None:
            try:
                await end_call(config, target_sid)
            except Exception:
                pass
        return f"Failed to warm-transfer call: {get_error_message(exc)}"

    return f"Transferring call to {resolved_destination} with introduction."
