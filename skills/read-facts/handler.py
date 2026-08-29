"""Operational handler for read-facts."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.db import get_active_facts_by_person, find_people
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
        if not people:
            return f'No person found matching "{person_query}".'
        person_id = people[0].id

    if not person_id:
        return "Please specify a person to look up facts for."

    facts = get_active_facts_by_person(db, person_id, 20)
    if not facts:
        return "No facts recorded for this person."

    formatted = "\n".join(
        f"- {fact.content} ({fact.type}, confidence: {fact.confidence:.2f})" for fact in facts
    )
    return f"Known facts:\n{formatted}"
