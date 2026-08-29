"""Operational handler for read-soul."""

from __future__ import annotations

from typing import Mapping

from mystic.config import read_soul
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    _params: Mapping[str, object],
) -> str:
    try:
        return read_soul()
    except OSError:
        return "SOUL.md not found."
