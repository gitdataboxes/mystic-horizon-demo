"""Operational handler for read-faq."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.memory import hybrid_search
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    query = params.get("query")
    if not isinstance(query, str) or not query:
        return "Please provide a question to search the FAQ."

    results = await hybrid_search(db, "faq", query, None, 3)
    if not results:
        return "No FAQ entries found for that question."
    return "\n\n---\n\n".join(result.content for result in results)
