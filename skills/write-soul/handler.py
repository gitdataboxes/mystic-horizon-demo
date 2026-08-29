"""Operational handler for write-soul."""

from __future__ import annotations

from typing import Mapping

from mystic.config import write_soul
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    content = params.get("content")
    if not isinstance(content, str) or not content.strip():
        return "Content is required for writing the soul."

    write_soul(content.strip(), trigger="write-soul")
    return "Soul written! Your values and personality are now saved."
