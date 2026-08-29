"""Operational handler for edit-person."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.db import update_person_name
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    phone = params.get("phone")
    name = params.get("name")
    if not isinstance(phone, str) or not phone:
        return "Please provide the person's phone number."
    if not isinstance(name, str) or not name:
        return "Please provide the new name."

    person = update_person_name(db, phone, name)
    if person is None:
        return f"No person found with phone: {phone}"
    return f"Updated name for {phone} to: {name}"
