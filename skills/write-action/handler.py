"""Operational handler for write-action."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.config import get_calendar_hub_config, get_error_message, logger
from mystic.calendar import create_hub_event
from mystic.db import format_due_at, parse_due_at, insert_action
from mystic.types import Action, OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    intent = params.get("intent")
    due = params.get("due")
    start_raw = params.get("start_at")
    end_raw = params.get("end_at")
    if not isinstance(intent, str) or not intent:
        return "Please provide an action intent."

    due_at: int | None = None
    if isinstance(due, str) and due:
        due_at = parse_due_at(due)
        if due_at is None:
            return (
                f'Could not parse due date: "{due}". '
                "Please use ISO format or a clear date/time."
            )

    start_at: int | None = None
    end_at: int | None = None
    if start_raw is not None or end_raw is not None:
        if not isinstance(start_raw, str) or not start_raw.strip():
            return "Please provide start_at in ISO 8601 format."
        if not isinstance(end_raw, str) or not end_raw.strip():
            return "Please provide end_at in ISO 8601 format."
        start_at = parse_due_at(start_raw)
        end_at = parse_due_at(end_raw)
        if start_at is None or end_at is None:
            return "Could not parse appointment time. Please use ISO 8601 timestamps."
        if end_at <= start_at:
            return "Please provide an end_at time after start_at."

    hub_pending = (
        start_at is not None
        and end_at is not None
        and get_calendar_hub_config() is not None
    )
    action = insert_action(
        db,
        person_id=ctx.person_id,
        call_id=ctx.call_id,
        intent=intent,
        due_at=due_at,
        source=ctx.source,
        start_at=start_at,
        end_at=end_at,
        hub_sync_status="pending" if hub_pending else None,
    )
    if hub_pending:
        await _try_hub_create(db, action)
    if start_at is not None and end_at is not None:
        return (
            f"Created scheduled action: {intent} — "
            f"{format_due_at(start_at)} to {format_due_at(end_at)} "
            f"(id: {action.id[:8]})"
        )
    return f"Created action: {intent} — due: {format_due_at(due_at)} (id: {action.id[:8]})"


async def _try_hub_create(db: sqlite3.Connection, action: Action) -> None:
    try:
        await create_hub_event(db, action)
    except Exception as exc:
        logger.warn("hub.create.error", action_id=action.id, error=get_error_message(exc))
