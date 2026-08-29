"""Operational handler for edit-config."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping, cast
from zoneinfo import ZoneInfo

from mystic.config import ConfigFilename, load_config, write_config
from mystic.types import OperationalContext

CONFIG_ALLOWLIST: dict[str, list[str]] = {
    "agent": ["agent.name", "hours.start", "hours.end", "hours.timezone", "hours.days"],
    "intelligence": [
        "retrieval.vectorWeight",
        "retrieval.ftsWeight",
        "retrieval.threshold",
        "retrieval.limit",
    ],
    "providers": [],
}

DAY_TOKENS = {
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


def is_field_editable(config_file: str, field_path: str) -> bool:
    allowed = CONFIG_ALLOWLIST.get(config_file)
    if allowed is None:
        return False
    for pattern in allowed:
        if pattern.endswith(".*") and field_path.startswith(pattern[:-1]):
            return True
        if pattern == field_path:
            return True
    return False


def _coerce_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _validate_timezone(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.now(ZoneInfo(value.strip()))
    except Exception:
        return None
    return value.strip()


def _validate_value(config_file: str, field_path: str, value: object) -> tuple[bool, object | str]:
    key = f"{config_file}.{field_path}"
    if key == "agent.agent.name":
        if not isinstance(value, str) or not value.strip():
            return False, "agent.name must be a non-empty string."
        return True, value.strip()

    if key in {"agent.hours.start", "agent.hours.end"}:
        number = _coerce_number(value)
        if not isinstance(number, int) or number < 0 or number > 23:
            return False, f"{field_path} must be an integer from 0 to 23."
        return True, number

    if key == "agent.hours.timezone":
        timezone = _validate_timezone(value)
        if timezone is None:
            return False, "hours.timezone must be a valid IANA timezone."
        return True, timezone

    if key == "agent.hours.days":
        if not isinstance(value, list) or not value:
            return False, "hours.days must be a non-empty array of day tokens."
        normalized: list[str] = []
        for item in cast(list[object], value):
            normalized.append(str(item).strip().lower())
        if any(item not in DAY_TOKENS for item in normalized):
            return False, "hours.days contains invalid day values."
        return True, normalized

    if key in {
        "intelligence.retrieval.vectorWeight",
        "intelligence.retrieval.ftsWeight",
        "intelligence.retrieval.threshold",
    }:
        number = _coerce_number(value)
        if not isinstance(number, (int, float)) or number < 0 or number > 1:
            return False, f"{field_path} must be a number between 0 and 1."
        return True, float(number)

    if key == "intelligence.retrieval.limit":
        number = _coerce_number(value)
        if not isinstance(number, int) or number < 1 or number > 100:
            return False, "retrieval.limit must be an integer between 1 and 100."
        return True, number

    return False, f"No validator for {field_path}."


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    config_file = params.get("file")
    field_path = params.get("path")
    value = params.get("value")

    if not isinstance(config_file, str) or not config_file:
        return "Please provide the config file name (e.g., agent, intelligence)."
    if not isinstance(field_path, str) or not field_path:
        return "Please provide the field path to edit."
    if value is None:
        return "Please provide the new value."

    if not is_field_editable(config_file, field_path):
        return f"Can't edit config — '{field_path}' is not an editable field in {config_file}.json"

    ok, validated = _validate_value(config_file, field_path, value)
    if not ok:
        return f"Can't edit config — {validated}"

    config_filename = cast(ConfigFilename, f"{config_file}.json")
    loaded = load_config(config_filename)
    config_data = asdict(loaded)

    current: dict[str, Any] = config_data
    parts = field_path.split(".")
    for key in parts[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            return f"Can't edit config — {key} is not an object in {config_file}.json"
        current = cast(dict[str, Any], next_value)

    current[parts[-1]] = validated
    write_config(config_filename, config_data)
    return f"Updated {config_file}.json: {field_path} = {validated!r}"
