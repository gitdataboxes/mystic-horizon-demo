"""Operational handler for write-person."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.config import validate_e164
from mystic.db import get_person_by_phone, upsert_person
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    phone = params.get("phone")
    name = params.get("name")
    if not isinstance(phone, str) or not phone:
        return "Please provide a phone number."

    try:
        validate_e164(phone)
    except ValueError:
        return "Invalid phone number format. Please use E.164 format (e.g., +15551234567)."

    existing = get_person_by_phone(db, phone)
    person = upsert_person(db, phone, name if isinstance(name, str) and name else None)
    verb = "Updated" if existing is not None else "Created"
    return f"{verb} contact: {person.name or 'Unknown'} ({person.phone})"
