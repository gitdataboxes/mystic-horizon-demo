"""Operational handler for find-open-slots."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from mystic.calendar import find_open_slots
from mystic.config import get_agent_config
from mystic.db import parse_due_at
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    start = params.get("start")
    end = params.get("end")
    if not isinstance(start, str) or not start.strip():
        return "Please provide a start time in ISO 8601 format."
    if not isinstance(end, str) or not end.strip():
        return "Please provide an end time in ISO 8601 format."

    start_ms = parse_due_at(start)
    end_ms = parse_due_at(end)
    if start_ms is None or end_ms is None:
        return "Could not parse the requested time range. Please use ISO 8601 timestamps."
    if end_ms <= start_ms:
        return "Please provide an end time after the start time."

    min_duration = params.get("min_duration_minutes")
    if isinstance(min_duration, int):
        min_duration_minutes = min_duration
    elif isinstance(min_duration, str) and min_duration.strip().isdigit():
        min_duration_minutes = int(min_duration.strip())
    else:
        min_duration_minutes = 30
    if min_duration_minutes <= 0:
        return "Please provide a positive minimum duration in minutes."

    slots = find_open_slots(
        db,
        start_ms,
        end_ms,
        min_duration_ms=min_duration_minutes * 60_000,
    )
    if not slots:
        return "No open slots found in that range."

    tz = ZoneInfo(get_agent_config().hours.timezone)
    lines = [f"- {_format_slot(slot_start, slot_end, tz)}" for slot_start, slot_end in slots]
    return "Open slots:\n" + "\n".join(lines)


def _format_slot(start_ms: int, end_ms: int, tz: ZoneInfo) -> str:
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=tz)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=tz)
    start_text = f"{start_dt.strftime('%a %b')} {start_dt.day} at {start_dt.strftime('%I:%M %p')}"
    end_text = end_dt.strftime('%I:%M %p')
    return f"{start_text} to {end_text}"
