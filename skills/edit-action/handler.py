"""Operational handler for edit-action."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Mapping, cast

from mystic.types import ActionStatus, OperationalContext
from mystic.db import parse_due_at, get_action_by_id, update_action_due_at, update_action_status


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    action_id = params.get("id")
    if not isinstance(action_id, str) or not action_id:
        return "Please provide an action ID."

    action = get_action_by_id(db, action_id)
    if action is None:
        return f"Action not found: {action_id}"

    status = params.get("status")
    due = params.get("due")
    if isinstance(status, str) and status:
        result = params.get("result")
        update_action_status(
            db,
            action_id,
            cast(ActionStatus, status),
            result if isinstance(result, str) else None,
        )
        return f"Action {action_id[:8]} status updated to: {status}"

    if isinstance(due, str) and due:
        due_at = parse_due_at(due)
        if due_at is None:
            return f'Could not parse due date: "{due}"'
        update_action_due_at(db, action_id, due_at)
        due_display = datetime.fromtimestamp(due_at / 1000).isoformat(sep=" ", timespec="minutes")
        return f"Action {action_id[:8]} due date updated to: {due_display}"

    return "Please provide status or due date to update."
