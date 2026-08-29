"""Operational handler for chat."""

from __future__ import annotations

from typing import Mapping

from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return "No message provided."
    return message.strip()
