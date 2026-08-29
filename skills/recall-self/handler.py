"""Operational handler for recall-self."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping

from mystic.config import list_journal_entries, read_journal_entry
from mystic.types import OperationalContext

_VALID_FILE_TYPES = frozenset({"soul", "identity"})
_FILE_LABELS = {"soul": "SOUL.md", "identity": "IDENTITY.md"}


def _parse_file_type(value: object) -> str | None:
    if value is None:
        return "soul"
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized not in _VALID_FILE_TYPES:
        return None
    return normalized


def _parse_timestamp(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _format_timestamp(timestamp: int) -> str:
    dt = datetime.fromtimestamp(timestamp / 1000, tz=UTC).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    file_type = _parse_file_type(params.get("file_type"))
    if file_type is None:
        return "Please choose file_type 'soul' or 'identity'."

    timestamp_value = params.get("timestamp")
    if timestamp_value is None:
        entries = list_journal_entries(file_type)
        if not entries:
            return f"No journal entries found for {_FILE_LABELS[file_type]}."

        lines = [f"Recent journal entries for {_FILE_LABELS[file_type]}:"]
        for entry in entries:
            line = (
                f"- {_format_timestamp(entry.timestamp)} | trigger={entry.trigger} "
                f"| timestamp={entry.timestamp}"
            )
            if entry.note:
                line += f" | note={entry.note}"
            lines.append(line)
        return "\n".join(lines)

    timestamp = _parse_timestamp(timestamp_value)
    if timestamp is None:
        return "Please provide a numeric journal timestamp."

    entry = read_journal_entry(file_type, timestamp)
    if entry is None:
        return f"No journal entry found for {_FILE_LABELS[file_type]} at {timestamp}."

    lines = [
        f"{_FILE_LABELS[file_type]} snapshot from {_format_timestamp(entry.timestamp)}",
        f"Trigger: {entry.trigger}",
        f"Timestamp: {entry.timestamp}",
    ]
    if entry.note:
        lines.append(f"Note: {entry.note}")
    lines.extend(["", entry.content])
    return "\n".join(lines)
