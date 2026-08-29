"""Operational handler for read-actions."""

from __future__ import annotations

import sqlite3
from typing import Mapping, cast

from mystic.types import ActionStatus, OperationalContext
from mystic.db import format_due_at, get_actions_by_status, get_all_pending_actions, get_pending_actions_by_person


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    status = params.get("status")
    status_name = status if isinstance(status, str) and status else "pending"
    if status_name == "pending":
        actions = (
            get_all_pending_actions(db)
            if ctx.audience == "owner"
            else get_pending_actions_by_person(db, ctx.person_id)
        )
    else:
        actions = get_actions_by_status(db, cast(ActionStatus, status_name))

    if not actions:
        return f"No {status_name} actions found."

    formatted = "\n".join(
        f"- [{action.id[:8]}] {action.intent} — due: {format_due_at(action.due_at)}, attempts: {action.attempts}/{action.max_attempts}"
        for action in actions
    )
    return f"{status_name.capitalize()} actions:\n{formatted}"
