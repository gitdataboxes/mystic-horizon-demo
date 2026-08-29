"""Cognitive handler for design-dashboard."""

from __future__ import annotations

from typing import Mapping

from mystic.config import read_dashboard_file, write_dashboard_file
from mystic.llm import invoke_agent


async def execute(
    system_prompt: str,
    data: str,
    params: Mapping[str, object],
    _options: Mapping[str, object],
) -> str:
    file_name = params.get("file")
    instruction = params.get("instructions", data)
    content = params.get("content")

    if not isinstance(file_name, str) or not file_name.strip():
        return "Please provide a dashboard file path."
    normalized = file_name.strip()

    if normalized.startswith("pages/") and not normalized.endswith(".html"):
        return f"Dashboard pages must be .html files, got: {normalized}"

    if isinstance(content, str) and content.strip():
        write_dashboard_file(normalized, content)
        return f"Updated dashboard file: {normalized}"

    if not isinstance(instruction, str) or not instruction.strip():
        return "Please provide instructions for the dashboard update."

    try:
        current_content = read_dashboard_file(normalized)
    except FileNotFoundError:
        current_content = ""

    result = await invoke_agent(
        "design-dashboard",
        system_prompt,
        f"Current {normalized}:\n\n{current_content}\n\n---\n\nInstruction: {instruction}",
    )
    write_dashboard_file(normalized, result, note=instruction)
    return f"Updated dashboard file: {normalized}"
