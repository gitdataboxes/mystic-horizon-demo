from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from mystic.db import (
    get_all_active_facts_by_person,
    get_call_by_id,
    get_chunks_with_null_embeddings,
    get_day_summary,
    get_facts_with_null_embeddings,
    get_calls_needing_extraction,
    insert_call,
    insert_fact,
    insert_transcript_chunk,
    update_call_transcript,
    upsert_person,
)
from mystic.memory import run_extraction_pipeline, run_nightly_extraction, run_retries
from tests.integration.helpers import PUBLIC_PHONE, SAMPLE_TRANSCRIPT, make_cognitive_skill_runner
from tests.python_helpers import make_embedding


async def test_failed_extraction_marks_call_and_surfaces_retry_candidate(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Casey")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    update_call_transcript(integration_env.db, call.id, SAMPLE_TRANSCRIPT)

    with (
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner(
                    {
                        "summarize-call": '{"summary":"Call summary."}',
                        "extract-facts": ValueError("facts extraction failed"),
                        "extract-commitments": '{"commitments":[]}',
                        "summarize-person": '{"summary":"Casey summary."}',
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
            new=AsyncMock(return_value=[]),
        ),
    ):
        await run_extraction_pipeline(integration_env.db, call.id, person.id, SAMPLE_TRANSCRIPT)

    updated = get_call_by_id(integration_env.db, call.id)
    assert updated is not None
    assert "facts" in (updated.extraction_error or "")
    assert updated.last_extraction_attempt_at is not None

    pending = get_calls_needing_extraction(integration_env.db)
    assert [candidate.id for candidate in pending] == [call.id]


async def test_run_retries_replays_partial_calls_and_reembeds_missing_vectors(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Jordan")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    integration_env.db.execute(
        "UPDATE calls SET started_at = ? WHERE id = ?",
        (int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000), call.id),
    )
    update_call_transcript(integration_env.db, call.id, SAMPLE_TRANSCRIPT)
    integration_env.db.commit()

    chunk = insert_transcript_chunk(
        integration_env.db,
        call_id=call.id,
        person_id=person.id,
        content="Chunk missing an embedding",
        chunk_index=0,
        embedding=None,
    )
    fact = insert_fact(
        integration_env.db,
        person_id=person.id,
        type="context",
        content="Fact missing an embedding",
        confidence=0.7,
        source="post-call",
        embedding=None,
    )

    with (
        patch("mystic.memory.run_nightly_extraction", new=AsyncMock(return_value=None)) as rerun,
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(return_value=[make_embedding([0.9, 0.2])]),
        ),
    ):
        await run_retries(integration_env.db)

    rerun.assert_awaited_once_with(integration_env.db, "2026-03-11")
    assert get_chunks_with_null_embeddings(integration_env.db) == []
    assert get_facts_with_null_embeddings(integration_env.db) == []
    refreshed_fact = get_all_active_facts_by_person(integration_env.db, person.id)[0]
    assert refreshed_fact.id == fact.id
    assert refreshed_fact.embedding is not None
    rows = integration_env.db.execute(
        "SELECT embedding FROM transcript_chunks WHERE id = ?",
        (chunk.id,),
    ).fetchall()
    assert rows[0]["embedding"] is not None


async def test_run_nightly_extraction_creates_day_summary(
    integration_env,
) -> None:
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Jordan")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    integration_env.db.execute(
        "UPDATE calls SET started_at = ? WHERE id = ?",
        (int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000), call.id),
    )
    integration_env.db.commit()
    update_call_transcript(integration_env.db, call.id, SAMPLE_TRANSCRIPT)

    with (
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner(
                    {
                        "summarize-call": '{"summary":"Jordan had one productive day of follow-up."}',
                        "extract-facts": '{"facts":[]}',
                        "extract-commitments": '{"commitments":[]}',
                        "summarize-person": '{"summary":"Jordan summary."}',
                    }
                )
            ),
        ),
        patch("mystic.memory.embed_chunks", new=AsyncMock(return_value=[])),
    ):
        await run_nightly_extraction(integration_env.db, "2026-03-11")

    summary = get_day_summary(integration_env.db, person.id, "2026-03-11")
    assert summary is not None
    assert summary.summary == "Jordan had one productive day of follow-up."
