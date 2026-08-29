"""Operational handler for read-calendar."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from mystic.config import get_agent_config
from mystic.db import get_upcoming_external_events, get_upcoming_scheduled_actions
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    days = params.get("days")
    if isinstance(days, int):
        range_days = days
    elif isinstance(days, str) and days.strip().isdigit():
        range_days = int(days.strip())
    else:
        range_days = 7
    if range_days <= 0:
        return "Please provide a positive number of days."

    keyword = params.get("query")
    keyword_text = keyword.strip().lower() if isinstance(keyword, str) and keyword.strip() else ""
    window_ms = range_days * 24 * 60 * 60_000
    tz = ZoneInfo(get_agent_config().hours.timezone)

    lines: list[str] = []
    if ctx.audience == "owner":
        for event in get_upcoming_external_events(db, within_ms=window_ms, limit=50):
            if keyword_text and keyword_text not in event.title.lower():
                continue
            lines.append(_format_event_line(event.start_at, event.title, tz, event.location, kind="calendar"))

    for action in get_upcoming_scheduled_actions(db, within_ms=window_ms, limit=50):
        if action.start_at is None:
            continue
        if ctx.audience != "owner" and action.person_id != ctx.person_id:
            continue
        if keyword_text and keyword_text not in action.intent.lower():
            continue
        lines.append(_format_event_line(action.start_at, action.intent, tz, None, kind="scheduled"))

    if not lines:
        if ctx.audience == "owner":
            return "No upcoming calendar items found."
        return "You do not have any upcoming scheduled appointments."

    heading = "Upcoming schedule:" if ctx.audience == "owner" else "Your upcoming appointments:"
    return f"{heading}\n" + "\n".join(lines)


def _format_event_line(
    timestamp_ms: int,
    title: str,
    tz: ZoneInfo,
    location: str | None,
    *,
    kind: str,
) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=tz)
    time_text = f"{dt.strftime('%a %b')} {dt.day} at {dt.strftime('%I:%M %p')}"
    suffix = f" ({location})" if location else ""
    return f"- {time_text}: {title}{suffix} [{kind}]"
