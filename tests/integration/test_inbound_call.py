from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

from mystic.calls import handle_end_of_call_report, handle_incoming_call, set_extraction_pipeline
from mystic.db import get_actions_by_call_id, get_call_by_external_id, get_all_active_facts_by_person, get_person_by_phone, get_chunks_by_call_id
from mystic.memory import run_extraction_pipeline
from tests.integration.helpers import (
    OWNER_PHONE,
    PUBLIC_PHONE,
    SAMPLE_TRANSCRIPT,
    make_cognitive_skill_runner,
    make_transcript_embeddings,
)
from tests.python_helpers import make_embedding


async def test_inbound_call_flow_persists_call_and_runs_extraction(
    integration_env,
) -> None:
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
        patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-inbound")),
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner(
                    {
                        "summarize-call": '{"summary":"Caller scheduled a Tuesday afternoon follow-up."}',
                        "extract-facts": (
                            '{"facts":[{"content":"Prefers Tuesday afternoon follow-ups",'
                            '"type":"preference","confidence":0.82,'
                            '"source_text":"Tuesday afternoon works best"}]}'
                        ),
                        "extract-commitments": (
                            '{"commitments":[{"content":"Send the quarterly report by Friday",'
                            '"intent":"Send the quarterly report","due":null,"urgency":"normal"}]}'
                        ),
                        "summarize-person": '{"summary":"Caller prefers Tuesday afternoon follow-ups."}',
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
            new=AsyncMock(return_value=[make_embedding([0.7, 0.1])]),
        ),
    ):
        result = await handle_incoming_call(
            integration_env.db,
            PUBLIC_PHONE,
            "CA-inbound-integration-1",
            integration_env.tunnel_url,
        )
        assert "twiml" in result
        assert "<Stream" in result["twiml"]
        assert "token=" in result["twiml"]

        await handle_end_of_call_report(
            integration_env.db,
            "CA-inbound-integration-1",
            SAMPLE_TRANSCRIPT,
            42,
        )
        await asyncio.wait_for(extraction_done.wait(), timeout=1)

    person = get_person_by_phone(integration_env.db, PUBLIC_PHONE)
    call = get_call_by_external_id(integration_env.db, "CA-inbound-integration-1")
    assert person is not None
    assert call is not None
    assert call.summary == "Caller scheduled a Tuesday afternoon follow-up."
    assert call.facts_extracted == 1
    assert call.commitments_extracted == 1
    assert call.duration == 42

    facts = get_all_active_facts_by_person(integration_env.db, person.id)
    actions = get_actions_by_call_id(integration_env.db, call.id)
    chunks = get_chunks_by_call_id(integration_env.db, call.id)
    assert [fact.content for fact in facts] == ["Prefers Tuesday afternoon follow-ups"]
    assert [action.intent for action in actions if action.source == "post-call"] == [
        "Send the quarterly report"
    ]
    assert len(chunks) >= 1
    assert chunks[0].embedding is not None


async def test_inbound_call_marks_owner_audience_and_passes_room_metadata(
    integration_env,
) -> None:
    create_room = AsyncMock(return_value="lk-room-owner")

    with patch("mystic.calls.create_room", new=create_room):
        result = await handle_incoming_call(
            integration_env.db,
            OWNER_PHONE,
            "CA-owner-integration-1",
            integration_env.tunnel_url,
        )

    assert "twiml" in result
    call = get_call_by_external_id(integration_env.db, "CA-owner-integration-1")
    person = get_person_by_phone(integration_env.db, OWNER_PHONE)
    assert call is not None
    assert person is not None
    assert call.audience == "owner"

    assert create_room.await_args is not None
    args = create_room.await_args.args
    metadata = args[2]
    assert metadata["callId"] == call.id
    assert metadata["personId"] == person.id
    assert metadata["audience"] == "owner"
    assert metadata["direction"] == "inbound"
