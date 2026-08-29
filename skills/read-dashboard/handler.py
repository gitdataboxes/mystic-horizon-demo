"""Operational handler for read-dashboard."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.config import list_dashboard_files, read_dashboard_file
from mystic.types import OperationalContext


async def execute(
    _db: sqlite3.Connection,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    file_name = params.get("file") or params.get("query")
    if isinstance(file_name, str) and file_name.strip():
        normalized = file_name.strip()
        try:
            content = read_dashboard_file(normalized)
        except FileNotFoundError:
            return f"Dashboard file not found: {normalized}"
        return f"Dashboard file {normalized}:\n\n{content}"

    files = list_dashboard_files()
    if not files:
        return "No dashboard files found."
    formatted = "\n".join(f"- {name}" for name in files)
    return f"Dashboard files:\n{formatted}"
