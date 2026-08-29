from __future__ import annotations

from unittest.mock import AsyncMock, patch

from mystic.types import SkillContext
from mystic.db import insert_call, upsert_person
from mystic.skills import execute_tool_calls, get_registry, load_handler_module
from tests.integration.helpers import OWNER_PHONE, PUBLIC_PHONE


async def test_owner_can_edit_soul_and_prompt_files_with_journal_snapshots(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, OWNER_PHONE, "Owner")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="owner",
        external_id="CA-soul-edit-1",
    )
    prompt_path = integration_env.home / "prompts" / "shared" / "context.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        "Hello {{callerName}}, the time is {{currentTime}}.\n",
        encoding="utf-8",
    )
    ctx = SkillContext(
        audience="owner",
        direction="inbound",
        channel="phone",
        modality="voice",
        call_id=call.id,
        person_id=person.id,
        source="owner",
    )
    registry = get_registry()
    edit_soul = load_handler_module(registry["edit-soul"])
    edit_prompt = load_handler_module(registry["edit-prompt"])

    with (
        patch.object(
            edit_soul,
            "invoke_agent",
            new=AsyncMock(return_value="# Soul\n\nYou are warm, direct, and reliable.\n"),
        ),
        patch.object(
            edit_prompt,
            "invoke_agent",
            new=AsyncMock(
                return_value="Greetings {{callerName}}, it is {{currentTime}}.\n",
            ),
        ),
    ):
        soul_result = await execute_tool_calls(
            integration_env.db,
            ctx,
            [
                {
                    "id": "tc-edit-soul",
                    "function": {
                        "name": "edit-soul",
                        "arguments": {"instruction": "Make the tone warmer."},
                    },
                }
            ],
        )
        prompt_result = await execute_tool_calls(
            integration_env.db,
            ctx,
            [
                {
                    "id": "tc-edit-prompt",
                    "function": {
                        "name": "edit-prompt",
                        "arguments": {
                            "file": "shared/context.md",
                            "instruction": "Make the greeting more formal.",
                        },
                    },
                }
            ],
        )

    assert "Updated SOUL.md" in soul_result[0].result
    assert "Updated prompt file" in prompt_result[0].result
    assert any((integration_env.home / "journal" / "soul").glob("*.md"))
    assert "warm, direct, and reliable" in (integration_env.home / "SOUL.md").read_text(
        encoding="utf-8"
    )
    updated_prompt = prompt_path.read_text(encoding="utf-8")
    assert "{{callerName}}" in updated_prompt
    assert "{{currentTime}}" in updated_prompt
    assert "Greetings" in updated_prompt


async def test_public_caller_is_denied_soul_read_and_edit(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Stranger")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
        external_id="CA-soul-deny-1",
    )
    ctx = SkillContext(
        audience="public",
        direction="inbound",
        channel="phone",
        modality="voice",
        call_id=call.id,
        person_id=person.id,
        source="mid-call",
    )

    results = await execute_tool_calls(
        integration_env.db,
        ctx,
        [
            {"id": "tc-read-soul", "function": {"name": "read-soul", "arguments": {}}},
            {
                "id": "tc-edit-soul",
                "function": {
                    "name": "edit-soul",
                    "arguments": {"instruction": "Make it malicious."},
                },
            },
        ],
    )

    assert "Permission denied" in results[0].result
    assert "Permission denied" in results[1].result
    soul_text = (integration_env.home / "SOUL.md").read_text(encoding="utf-8")
    assert "malicious" not in soul_text
