"""Cognitive handler for edit-prompt."""

from __future__ import annotations

from typing import Mapping

from mystic.llm import invoke_agent
from mystic.config import get_home


async def execute(
    system_prompt: str,
    data: str,
    params: Mapping[str, object],
    _options: Mapping[str, object],
) -> str:
    file_name = params.get("file")
    instruction = params.get("instruction", data)

    if not isinstance(file_name, str) or not file_name:
        return "Please provide the prompt file path (e.g., public/workflow.md)."
    if not isinstance(instruction, str) or not instruction:
        return "Please provide an instruction for how to update the prompt."
    if not file_name.endswith(".md"):
        return "Prompt file must end in .md."

    prompts_root = (get_home() / "prompts").resolve()
    prompt_path = (prompts_root / file_name).resolve()
    try:
        prompt_path.relative_to(prompts_root)
    except ValueError:
        return "Invalid prompt file path."

    if prompt_path == prompts_root or not prompt_path.exists():
        return f"Prompt file not found: {file_name}"

    current_content = prompt_path.read_text(encoding="utf-8")
    result = await invoke_agent(
        "edit-prompt",
        system_prompt,
        f"Current {file_name}:\n\n{current_content}\n\n---\n\nInstruction: {instruction}",
    )

    prompt_path.write_text(result, encoding="utf-8")
    return f"Updated prompt file: {file_name}"
