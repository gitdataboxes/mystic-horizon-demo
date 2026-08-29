from __future__ import annotations

from mystic.actions import start_action_attempt
from mystic.config import get_agent_config, read_identity, read_soul
from mystic.types import SkillContext
from mystic.calls import handle_end_of_call_report, handle_unanswered_outbound, set_call_ended_callback
from mystic.db import insert_action, insert_call, upsert_person
from mystic.skills import execute_tool_calls
from tests.integration.helpers import OWNER_PHONE, SAMPLE_TRANSCRIPT


async def test_bootstrap_write_skills_update_identity_soul_and_agent_config(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, OWNER_PHONE, "Owner")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="outbound",
        audience="owner",
        external_id="CA-bootstrap-write-1",
    )
    ctx = SkillContext(
        audience="owner",
        direction="outbound",
        channel="phone",
        modality="voice",
        call_id=call.id,
        person_id=person.id,
        source="owner",
    )

    identity_result = await execute_tool_calls(
        integration_env.db,
        ctx,
        [
            {
                "id": "tc-write-identity",
                "function": {
                    "name": "write-identity",
                    "arguments": {
                        "name": "Nova",
                        "creature": "digital familiar",
                        "vibe": "warm and curious",
                        "emoji": "*",
                    },
                },
            }
        ],
    )
    soul_result = await execute_tool_calls(
        integration_env.db,
        ctx,
        [
            {
                "id": "tc-write-soul",
                "function": {
                    "name": "write-soul",
                    "arguments": {
                        "content": "# Soul\n\nI am warm, curious, and practical.\n",
                    },
                },
            }
        ],
    )

    assert "Identity written" in identity_result[0].result
    assert "Soul written" in soul_result[0].result
    identity = read_identity()
    assert identity.name == "Nova"
    assert identity.creature == "digital familiar"
    assert "warm, curious, and practical" in read_soul()
    assert get_agent_config().agent.name == "Nova"


async def test_bootstrap_callback_reports_answered_and_unanswered_calls(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, OWNER_PHONE, "Owner")
    action = insert_action(
        integration_env.db,
        person_id=person.id,
        intent="Get to know owner",
        source="cli",
    )
    start_action_attempt(integration_env.db, action.id)
    answered_call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="outbound",
        audience="owner",
        external_id="CA-bootstrap-answered-1",
    )
    unanswered_call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="outbound",
        audience="owner",
        external_id="CA-bootstrap-unanswered-1",
        action_id=action.id,
    )
    seen: list[tuple[str, str, bool]] = []
    set_call_ended_callback(lambda call_id, external_id, answered: seen.append((call_id, external_id, answered)))

    await handle_end_of_call_report(
        integration_env.db,
        answered_call.external_id or answered_call.id,
        SAMPLE_TRANSCRIPT,
        15,
    )
    handle_unanswered_outbound(
        integration_env.db,
        unanswered_call.external_id or unanswered_call.id,
    )

    assert seen == [
        (answered_call.id, "CA-bootstrap-answered-1", True),
        (unanswered_call.id, "CA-bootstrap-unanswered-1", False),
    ]
