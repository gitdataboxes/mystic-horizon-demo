"""Operational handler for supersede-fact."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.types import OperationalContext
from mystic.db import get_fact_by_id, supersede_fact


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    fact_id = params.get("id")
    if not isinstance(fact_id, str) or not fact_id:
        return "Please provide a fact ID."

    fact = get_fact_by_id(db, fact_id)
    if fact is None:
        return f"Fact not found: {fact_id}"

    if fact.superseded_at is not None:
        return f"Fact {fact_id[:8]} is already superseded."

    supersede_fact(db, fact_id)
    return f'Superseded fact {fact_id[:8]}: "{fact.content}"'
