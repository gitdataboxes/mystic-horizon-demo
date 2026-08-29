"""Operational handler for read-transcripts."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.memory import hybrid_search
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    query = params.get("query")
    if not isinstance(query, str) or not query:
        return "Please provide a search query for transcripts."

    person_id = ctx.person_id if ctx.audience == "public" else None
    results = await hybrid_search(db, "transcripts", query, person_id, 5)
    if not results:
        return "No transcript matches found for that query."

    formatted = "\n\n".join(
        f"{index}. {result.content[:300]}{'...' if len(result.content) > 300 else ''}"
        for index, result in enumerate(results, start=1)
    )
    return f"Found {len(results)} transcript excerpts:\n\n{formatted}"
