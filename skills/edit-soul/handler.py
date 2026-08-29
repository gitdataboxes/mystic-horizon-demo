"""Cognitive handler for edit-soul."""

from __future__ import annotations

from typing import Mapping

from mystic.llm import invoke_agent
from mystic.config import write_soul


async def execute(
    system_prompt: str,
    data: str,
    _params: Mapping[str, object],
    _options: Mapping[str, object],
) -> str:
    result = await invoke_agent("edit-soul", system_prompt, f"Instruction: {data}")
    write_soul(result, trigger="edit-soul")
    return "Updated SOUL.md. Previous version saved to journal."
