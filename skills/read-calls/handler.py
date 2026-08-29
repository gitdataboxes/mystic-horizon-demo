"""Operational handler for read-calls."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Mapping

from mystic.db import get_recent_calls_by_person, find_people
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    person_query = params.get("person")
    person_id: str | None = None
    if ctx.audience == "public":
        person_id = ctx.person_id
    elif isinstance(person_query, str) and person_query:
        people = find_people(db, person_query)
        if people:
            person_id = people[0].id

    target_person_id = person_id or ctx.person_id
    calls = get_recent_calls_by_person(db, target_person_id, 10)
    if not calls:
        return "No call history found."

    formatted = "\n".join(
        (
            f"- {datetime.fromtimestamp(call.started_at / 1000, tz=UTC).isoformat(timespec='minutes')} "
            f"({call.direction}, "
            f"{f'{round(call.duration / 60)}m' if call.duration is not None else 'ongoing'}): "
            f"{call.summary or 'no summary'}"
        )
        for call in calls
    )
    return f"Recent calls:\n{formatted}"
