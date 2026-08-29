"""Operational handler for take-message."""

from __future__ import annotations

import sqlite3
from typing import Mapping, cast

from mystic.actions import notify
from mystic.config import get_agent_config
from mystic.db import get_person_by_id, insert_action
from mystic.types import ActionUrgency, OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    content = params.get("content")
    if not isinstance(content, str) or not content.strip():
        return "Please provide a message to record."
    content = content.strip()

    urgency_raw = params.get("urgency")
    if urgency_raw is None:
        urgency: ActionUrgency = "normal"
    elif urgency_raw in {"normal", "high"}:
        urgency = cast(ActionUrgency, urgency_raw)
    else:
        return "Invalid urgency. Use 'normal' or 'high'."

    insert_action(
        db,
        person_id=ctx.person_id,
        call_id=ctx.call_id,
        intent=f"Message: {content}",
        urgency=urgency,
        source=ctx.source,
    )

    person = get_person_by_id(db, ctx.person_id)
    person_name = person.name if person and person.name else "the caller"
    await notify(
        get_agent_config().agent.name,
        f"Message from {person_name}: {content}",
    )
    return f"Got it - I've recorded this message: '{content}'. I'll make sure they get it."
