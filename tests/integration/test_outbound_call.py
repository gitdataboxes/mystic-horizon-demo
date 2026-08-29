from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

from mystic.calls import (
    get_active_call_count,
    handle_answered_outbound,
    handle_end_of_call_report,
    handle_unanswered_outbound,
    initiate_outbound_call,
    set_extraction_pipeline,
)
from mystic.db import get_action_by_id, insert_action, get_call_by_external_id, upsert_person
from mystic.memory import run_extraction_pipeline
from tests.integration.helpers import (
    PUBLIC_PHONE,
    SAMPLE_TRANSCRIPT,
    make_cognitive_skill_runner,
    make_transcript_embeddings,
)
from tests.python_helpers import make_embedding


async def test_outbound_call_flow_marks_answered_extracts_and_requeues_action(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Morgan")
    action = insert_action(
        integration_env.db,
        person_id=person.id,
        intent="Follow up on the quarterly report",
        source="agent",
    )
    extraction_done = asyncio.Event()

    async def extraction_pipeline(
        db: sqlite3.Connection,
        call_id: str,
        person_id: str,
        transcript: str,
    ) -> None:
        await run_extraction_pipeline(db, call_id, person_id, transcript)
        extraction_done.set()

    set_extraction_pipeline(extraction_pipeline)

    with (
        patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-outbound")),
        patch("mystic.calls.make_outbound_call", new=AsyncMock(return_value="CA-outbound-1")),
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner(
                    {
                        "summarize-call": '{"summary":"Report follow-up call completed."}',
                        "extract-facts": '{"facts":[]}',
                        "extract-commitments": '{"commitments":[]}',
                        "summarize-person": '{"summary":"Morgan is waiting on the quarterly report."}',
                    }
                )
            ),
        ),
        patch(
            "mystic.skills.execute_cognitive_skill",
            new=AsyncMock(return_value="[]"),
        ),
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(side_effect=make_transcript_embeddings),
        ),
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(return_value=[make_embedding([0.5])]),
        ),
    ):
        call_id = await initiate_outbound_call(integration_env.db, action, integration_env.tunnel_url)
        assert call_id is not None

        handle_answered_outbound(integration_env.db, "CA-outbound-1")
        await handle_end_of_call_report(
            integration_env.db,
            "CA-outbound-1",
            SAMPLE_TRANSCRIPT,
            63,
        )
        await asyncio.wait_for(extraction_done.wait(), timeout=1)

    call = get_call_by_external_id(integration_env.db, "CA-outbound-1")
    updated_action = get_action_by_id(integration_env.db, action.id)
    assert call is not None
    assert updated_action is not None
    assert call.answered_at is not None
    assert call.ended_at is not None
    assert call.duration == 63
    assert updated_action.status == "pending"
    assert updated_action.due_at is not None
    assert "Call completed without fully resolving the action." in (updated_action.result or "")
    assert get_active_call_count(integration_env.db) == 0


async def test_answered_outbound_call_is_not_later_marked_unanswered(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Taylor")
    action = insert_action(
        integration_env.db,
        person_id=person.id,
        intent="Confirm next steps",
        source="agent",
    )

    with (
        patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-answered")),
        patch("mystic.calls.make_outbound_call", new=AsyncMock(return_value="CA-answered-skip")),
    ):
        await initiate_outbound_call(integration_env.db, action, integration_env.tunnel_url)

    handle_answered_outbound(integration_env.db, "CA-answered-skip")
    handle_unanswered_outbound(integration_env.db, "CA-answered-skip")

    call = get_call_by_external_id(integration_env.db, "CA-answered-skip")
    updated_action = get_action_by_id(integration_env.db, action.id)
    assert call is not None
    assert updated_action is not None
    assert call.answered_at is not None
    assert call.ended_at is None
    assert updated_action.status == "in_progress"
    assert get_active_call_count(integration_env.db) == 1
