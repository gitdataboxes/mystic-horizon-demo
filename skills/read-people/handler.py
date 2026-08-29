"""Operational handler for read-people."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.db import find_people
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    query = params.get("query")
    if not isinstance(query, str) or not query:
        return "Please provide a search query for people."

    people = find_people(db, query)
    if not people:
        return f'No people found matching "{query}".'

    formatted = "\n".join(
        f"- {person.name or 'Unknown'} ({person.phone}){f': {person.summary}' if person.summary else ''}"
        for person in people
    )
    return f"Found {len(people)} people:\n{formatted}"
