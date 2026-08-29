from __future__ import annotations

from unittest.mock import AsyncMock, patch

from mystic.actions import check_satisfaction
from mystic.db import get_action_by_id, insert_action, insert_call, update_call_end, update_call_summary, upsert_person
from tests.integration.helpers import PUBLIC_PHONE, SAMPLE_TRANSCRIPT


async def test_satisfaction_updates_completed_partial_and_ignores_unknown_actions(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Robin")
    completed = insert_action(integration_env.db, person_id=person.id, intent="Schedule meeting", source="agent")
    partial = insert_action(integration_env.db, person_id=person.id, intent="Send report", source="agent")
    untouched = insert_action(integration_env.db, person_id=person.id, intent="Review proposal", source="agent")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    update_call_end(integration_env.db, call.id, transcript=SAMPLE_TRANSCRIPT)
    update_call_summary(integration_env.db, call.id, "Meeting confirmed, report discussed.")

    raw = (
        "["
        f'{{"id":"{completed.id}","status":"satisfied","confidence":0.95,"reason":"Meeting confirmed."}},'
        f'{{"id":"{partial.id}","status":"partial","confidence":0.64,"reason":"Report discussed but not sent."}},'
        '{"id":"missing-action","status":"satisfied","confidence":0.9,"reason":"Ignore me."}'
        "]"
    )
    with patch("mystic.skills.execute_cognitive_skill", new=AsyncMock(return_value=raw)):
        await check_satisfaction(integration_env.db, call.id, person.id)

    completed_action = get_action_by_id(integration_env.db, completed.id)
    partial_action = get_action_by_id(integration_env.db, partial.id)
    untouched_action = get_action_by_id(integration_env.db, untouched.id)
    assert completed_action is not None
    assert partial_action is not None
    assert untouched_action is not None
    assert completed_action.status == "completed"
    assert partial_action.status == "pending"
    assert "Partially addressed in call" in (partial_action.context or "")
    assert untouched_action.status == "pending"
    assert untouched_action.context is None


async def test_satisfaction_leaves_actions_unchanged_when_judgment_payload_is_invalid(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Avery")
    action = insert_action(integration_env.db, person_id=person.id, intent="Call back", source="agent")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    update_call_end(integration_env.db, call.id, transcript=SAMPLE_TRANSCRIPT)
    update_call_summary(integration_env.db, call.id, "No useful structure returned.")

    with patch("mystic.skills.execute_cognitive_skill", new=AsyncMock(return_value="not-json")):
        await check_satisfaction(integration_env.db, call.id, person.id)

    updated = get_action_by_id(integration_env.db, action.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.context is None
