"""Operational handler for read-search."""

from __future__ import annotations

from typing import Mapping

from mystic.llm import invoke_agent
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    query = params.get("query")
    if not isinstance(query, str) or not query:
        return "Please provide a search query."

    try:
        return await invoke_agent("read-search", "", query)
    except Exception as exc:
        return f"Search unavailable right now: {exc}"
