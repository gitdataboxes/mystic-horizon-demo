"""Operational handler for check-availability."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.calendar import check_availability
from mystic.db import parse_due_at
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
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

    is_free, conflicts = check_availability(db, start_ms, end_ms)
    if is_free:
        return "That time is available."
    if ctx.audience != "owner":
        return "That time is already booked."
    return "That time is not available. Conflicts: " + ", ".join(conflicts)
