"""Operational handler for manage-appointment."""

from __future__ import annotations

import sqlite3
from typing import Mapping
from zoneinfo import ZoneInfo

from mystic.calendar import create_hub_event, delete_hub_event, format_event_time, update_hub_event
from mystic.calls import send_sms
from mystic.config import get_agent_config, get_calendar_hub_config, get_error_message, get_twilio_config, logger
from mystic.db import (
    get_action_by_id,
    get_person_by_id,
    parse_due_at,
    update_action_due_at,
    update_action_status,
    update_action_time_slot,
)
from mystic.types import Action, OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    action_id = params.get("id")
    operation = params.get("operation")
    if not isinstance(action_id, str) or not action_id:
        return "Please provide an appointment ID."
    if not isinstance(operation, str) or not operation:
        return "Please provide an appointment operation."

    action = get_action_by_id(db, action_id)
    if action is None:
        return f"Appointment not found: {action_id}"
    if ctx.audience != "owner" and action.person_id != ctx.person_id:
        return "You can only manage your own appointments."

    op_name = operation.strip().lower()
    if op_name == "cancel":
        hub_pending = action.hub_event_id is not None or action.hub_sync_status is not None
        update_action_status(
            db,
            action.id,
            "cancelled",
            "Cancelled via manage-appointment.",
            hub_sync_status="pending" if hub_pending else None,
        )
        if action.hub_event_id:
            await _try_hub_delete(db, action)
        return f"Cancelled appointment: {action.intent}"

    if op_name != "reschedule":
        return "Unsupported appointment operation. Use cancel or reschedule."

    start_raw = params.get("start_at")
    end_raw = params.get("end_at")
    if not isinstance(start_raw, str) or not start_raw.strip():
        return "Please provide a new start_at time in ISO 8601 format."
    if not isinstance(end_raw, str) or not end_raw.strip():
        return "Please provide a new end_at time in ISO 8601 format."

    start_at = parse_due_at(start_raw)
    end_at = parse_due_at(end_raw)
    if start_at is None or end_at is None:
        return "Could not parse the new appointment time. Please use ISO 8601 timestamps."
    if end_at <= start_at:
        return "Please provide an end_at time after start_at."

    previous_start = action.start_at
    hub_pending = get_calendar_hub_config() is not None
    update_action_time_slot(
        db,
        action.id,
        start_at,
        end_at,
        hub_sync_status="pending" if hub_pending else None,
    )
    if previous_start is not None and action.due_at == previous_start:
        update_action_due_at(db, action.id, start_at)

    updated = get_action_by_id(db, action.id)
    if updated is not None and hub_pending:
        if updated.hub_event_id:
            await _try_hub_update(db, updated)
        else:
            await _try_hub_create(db, updated)
    await _maybe_send_confirmation_sms(db, action.person_id, action.intent, start_at, prefix="Updated")
    return f"Rescheduled appointment: {action.intent}"


async def _maybe_send_confirmation_sms(
    db: sqlite3.Connection,
    person_id: str | None,
    intent: str,
    start_at: int,
    *,
    prefix: str,
) -> None:
    if person_id is None:
        return
    person = get_person_by_id(db, person_id)
    if person is None or not person.phone:
        return
    twilio = get_twilio_config()
    if twilio is None:
        return

    try:
        tz = ZoneInfo(get_agent_config().hours.timezone)
        time_text = format_event_time(start_at, tz)
        await send_sms(twilio, person.phone, f"{prefix}: {intent} at {time_text}")
    except Exception:
        return


async def _try_hub_create(db: sqlite3.Connection, action: Action) -> None:
    try:
        await create_hub_event(db, action)
    except Exception as exc:
        logger.warn("hub.create.error", action_id=action.id, error=get_error_message(exc))


async def _try_hub_update(db: sqlite3.Connection, action: Action) -> None:
    try:
        await update_hub_event(db, action)
    except Exception as exc:
        logger.warn("hub.update.error", action_id=action.id, error=get_error_message(exc))


async def _try_hub_delete(db: sqlite3.Connection, action: Action) -> None:
    try:
        await delete_hub_event(db, action)
    except Exception as exc:
        logger.warn("hub.delete.error", action_id=action.id, error=get_error_message(exc))
