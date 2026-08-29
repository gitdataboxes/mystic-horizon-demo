from __future__ import annotations

from unittest.mock import AsyncMock, patch

from mystic.types import SkillContext
from mystic.db import insert_call, upsert_person
from mystic.memory import hybrid_search, index_transcript
from mystic.skills import execute_tool_calls
from tests.integration.helpers import ALT_PHONE, PUBLIC_PHONE, make_transcript_embeddings
from tests.python_helpers import make_embedding


async def test_transcript_search_is_person_scoped_before_ranking(
    integration_env,
) -> None:
    alice = upsert_person(integration_env.db, PUBLIC_PHONE, "Alice")
    bob = upsert_person(integration_env.db, ALT_PHONE, "Bob")
    alice_call = insert_call(
        integration_env.db,
        person_id=alice.id,
        direction="inbound",
        audience="public",
    )
    bob_call = insert_call(
        integration_env.db,
        person_id=bob.id,
        direction="inbound",
        audience="public",
    )

    with (
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(side_effect=make_transcript_embeddings),
        ),
        patch(
            "mystic.memory.embed_query",
            new=AsyncMock(return_value=make_embedding([1.0, 0.1])),
        ),
    ):
        await index_transcript(
            integration_env.db,
            alice_call.id,
            alice.id,
            "Alice confirmed the schedule meeting is on Tuesday afternoon.",
        )
        await index_transcript(
            integration_env.db,
            bob_call.id,
            bob.id,
            "Bob asked for the schedule meeting to move to Wednesday morning.",
        )
        results = await hybrid_search(
            integration_env.db,
            "transcripts",
            "schedule meeting afternoon",
            alice.id,
            2,
        )

    assert len(results) == 1
    assert "Alice confirmed" in results[0].content
    assert "Wednesday" not in results[0].content


async def test_read_skill_can_search_transcripts_with_real_indexed_chunks(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Jamie")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )

    with (
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(side_effect=make_transcript_embeddings),
        ),
        patch(
            "mystic.memory.embed_query",
            new=AsyncMock(return_value=make_embedding([1.0, 0.2])),
        ),
    ):
        await index_transcript(
            integration_env.db,
            call.id,
            person.id,
            "Jamie asked if the Tuesday afternoon follow-up could stay on the calendar.",
        )
        results = await execute_tool_calls(
            integration_env.db,
            SkillContext(
                audience="public",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id=call.id,
                person_id=person.id,
                source="mid-call",
            ),
            [
                {
                    "id": "tc-read-transcripts",
                    "function": {
                        "name": "read-transcripts",
                        "arguments": {"query": "Tuesday afternoon"},
                    },
                }
            ],
        )

    assert "Found 1 transcript excerpts" in results[0].result
    assert "Tuesday afternoon follow-up" in results[0].result
