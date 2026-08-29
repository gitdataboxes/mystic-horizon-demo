from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from mystic.actions import start_action_attempt
from mystic.config import read_soul, write_soul
from mystic.db import (
    close_database,
    get_action_by_id,
    get_all_active_facts_by_person,
    get_call_by_id,
    get_chunks_by_call_id,
    get_day_summary,
    get_pending_actions_by_person,
    get_person_by_id,
    initialize_schema,
    insert_action,
    insert_call,
    insert_fact,
    insert_transcript_chunk,
    open_database,
    update_call_transcript,
    upsert_person,
)
from mystic.memory import drain_retry_loop, run_extraction_pipeline, run_nightly_extraction, run_retries, start_retry_loop
from tests.python_helpers import TempAppHome, make_embedding, seed_core_files

OWNER_PHONE = "+15551234567"
PUBLIC_PHONE = "+15550002222"
SAMPLE_TRANSCRIPT = (
    "We scheduled the Tuesday meeting and you promised to send the report by Friday."
)


class ExtractionPhaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)

    async def asyncTearDown(self) -> None:
        await drain_retry_loop(1000)

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_run_extraction_pipeline_stores_summary_facts_actions_and_person_summary(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")
        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")

        async def fake_execute(skill_name: str, *_args: object, **_kwargs: object) -> str:
            responses = {
                "summarize-call": '{"summary":"Caller scheduled a Tuesday meeting."}',
                "extract-facts": (
                    '{"facts":[{"content":"Prefers Tuesday meetings","type":"preference",'
                    '"confidence":0.8,"source_text":"Tuesday works best"}]}'
                ),
                "extract-commitments": (
                    '{"commitments":[{"content":"Send the report by Friday","intent":"Send the report",'
                    '"due":"2026-03-13T17:00:00Z","urgency":"normal"}]}'
                ),
                "summarize-person": '{"summary":"Carol prefers Tuesday meetings."}',
            }
            return responses[skill_name]

        async def fake_transcript_embeddings(chunks: list[str]) -> list[list[float]]:
            return [make_embedding([float(index + 1)]) for index, _ in enumerate(chunks)]

        with (
            patch("mystic.memory.execute_cognitive_skill", new=AsyncMock(side_effect=fake_execute)),
            patch("mystic.memory.check_satisfaction", new=AsyncMock(return_value=None)) as satisfaction,
            patch("mystic.memory.embed_chunks", new=AsyncMock(return_value=[make_embedding([0.4])])),
            patch(
                "mystic.memory.embed_chunks",
                new=AsyncMock(side_effect=fake_transcript_embeddings),
            ),
        ):
            await run_extraction_pipeline(self.db, call.id, person.id, SAMPLE_TRANSCRIPT)

        updated_call = get_call_by_id(self.db, call.id)
        facts = get_all_active_facts_by_person(self.db, person.id)
        actions = get_pending_actions_by_person(self.db, person.id)
        updated_person = get_person_by_id(self.db, person.id)
        chunks = get_chunks_by_call_id(self.db, call.id)

        self.assertIsNotNone(updated_call)
        self.assertIsNotNone(updated_person)
        assert updated_call is not None
        assert updated_person is not None
        self.assertEqual(updated_call.summary, "Caller scheduled a Tuesday meeting.")
        self.assertEqual(updated_call.facts_extracted, 1)
        self.assertEqual(updated_call.commitments_extracted, 1)
        self.assertEqual(updated_person.summary, "Carol prefers Tuesday meetings.")
        self.assertEqual([fact.content for fact in facts], ["Prefers Tuesday meetings"])
        self.assertEqual([action.intent for action in actions], ["Send the report"])
        self.assertGreaterEqual(len(chunks), 1)
        satisfaction.assert_awaited_once_with(self.db, call.id, person.id)

    async def test_run_extraction_pipeline_records_phase_errors(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")
        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")

        async def fake_execute(skill_name: str, *_args: object, **_kwargs: object) -> str:
            responses = {
                "summarize-call": '{"summary":"Caller scheduled a Tuesday meeting."}',
                "extract-facts": "not-json",
                "extract-commitments": '{"commitments":[]}',
                "summarize-person": '{"summary":"Carol summary."}',
            }
            return responses[skill_name]

        with (
            patch("mystic.memory.execute_cognitive_skill", new=AsyncMock(side_effect=fake_execute)),
            patch("mystic.memory.check_satisfaction", new=AsyncMock(return_value=None)),
            patch("mystic.memory.embed_chunks", new=AsyncMock(return_value=[])),
        ):
            await run_extraction_pipeline(self.db, call.id, person.id, SAMPLE_TRANSCRIPT)

        updated_call = get_call_by_id(self.db, call.id)
        self.assertIsNotNone(updated_call)
        assert updated_call is not None
        self.assertIn("facts", updated_call.extraction_error or "")
        self.assertEqual(updated_call.facts_extracted, 0)
        self.assertEqual(updated_call.summary, "Caller scheduled a Tuesday meeting.")

    async def test_run_extraction_pipeline_writes_bootstrap_soul_fallback(self) -> None:
        write_soul("")
        owner = upsert_person(self.db, OWNER_PHONE, "Owner")
        action = insert_action(
            self.db,
            person_id=owner.id,
            intent="Get to know owner",
            context="Bootstrap: discover identity and soul.",
            source="cli",
        )
        call = insert_call(
            self.db,
            person_id=owner.id,
            direction="outbound",
            audience="owner",
            action_id=action.id,
        )

        async def fake_execute(skill_name: str, *_args: object, **_kwargs: object) -> str:
            if skill_name == "edit-soul":
                write_soul("# Soul\n\nI am calm and reliable.")
                return "# Soul\n\nI am calm and reliable."
            responses = {
                "summarize-call": '{"summary":"Bootstrap call."}',
                "extract-facts": '{"facts":[]}',
                "extract-commitments": '{"commitments":[]}',
                "summarize-person": '{"summary":"Owner summary."}',
            }
            return responses[skill_name]

        with (
            patch("mystic.memory.execute_cognitive_skill", new=AsyncMock(side_effect=fake_execute)),
            patch("mystic.memory.check_satisfaction", new=AsyncMock(return_value=None)),
            patch("mystic.memory.embed_chunks", new=AsyncMock(return_value=[])),
        ):
            await run_extraction_pipeline(self.db, call.id, owner.id, SAMPLE_TRANSCRIPT)

        self.assertIn("calm and reliable", read_soul())

    async def test_run_extraction_pipeline_dedupes_same_call(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")

        async def delayed(_db: object, _call_id: object, _person_id: object, _transcript: object) -> None:
            await asyncio.sleep(0.01)

        with patch("mystic.memory._run_extraction_pipeline_internal", new=AsyncMock(side_effect=delayed)) as internal:
            await asyncio.gather(
                run_extraction_pipeline(self.db, "call-1", person.id, SAMPLE_TRANSCRIPT),
                run_extraction_pipeline(self.db, "call-1", person.id, SAMPLE_TRANSCRIPT),
            )

        internal.assert_awaited_once()

    async def test_run_nightly_extraction_stores_day_summary_facts_actions_and_person_summary(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")
        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        self.db.execute(
            "UPDATE calls SET started_at = ? WHERE id = ?",
            (int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000), call.id),
        )
        update_call_transcript(self.db, call.id, SAMPLE_TRANSCRIPT)

        async def fake_execute(skill_name: str, *_args: object, **_kwargs: object) -> str:
            responses = {
                "summarize-call": '{"summary":"Carol had a productive Tuesday planning day."}',
                "extract-facts": (
                    '{"facts":[{"content":"Prefers Tuesday meetings","type":"preference",'
                    '"confidence":0.8,"source_text":"Tuesday works best"}]}'
                ),
                "extract-commitments": (
                    '{"commitments":[{"content":"Send the report by Friday","intent":"Send the report",'
                    '"due":"2026-03-13T17:00:00Z","urgency":"normal"}]}'
                ),
                "summarize-person": '{"summary":"Carol prefers Tuesday meetings."}',
            }
            return responses[skill_name]

        async def fake_embed(chunks: list[str]) -> list[list[float]]:
            return [make_embedding([float(index + 1)]) for index, _ in enumerate(chunks)]

        with (
            patch("mystic.memory.execute_cognitive_skill", new=AsyncMock(side_effect=fake_execute)),
            patch("mystic.memory.embed_chunks", new=AsyncMock(side_effect=fake_embed)),
        ):
            await run_nightly_extraction(self.db, "2026-03-11")

        summary = get_day_summary(self.db, person.id, "2026-03-11")
        facts = get_all_active_facts_by_person(self.db, person.id)
        actions = get_pending_actions_by_person(self.db, person.id)
        updated_person = get_person_by_id(self.db, person.id)
        chunks = get_chunks_by_call_id(self.db, call.id)

        self.assertIsNotNone(summary)
        self.assertIsNotNone(updated_person)
        assert summary is not None
        assert updated_person is not None
        self.assertEqual(summary.summary, "Carol had a productive Tuesday planning day.")
        self.assertEqual(summary.facts_extracted, 1)
        self.assertEqual(summary.commitments_extracted, 1)
        self.assertEqual(updated_person.summary, "Carol prefers Tuesday meetings.")
        self.assertEqual([fact.content for fact in facts], ["Prefers Tuesday meetings"])
        self.assertEqual([action.intent for action in actions], ["Send the report"])
        self.assertGreaterEqual(len(chunks), 1)

    async def test_run_retries_replays_day_extraction_and_reembeds_missing_vectors(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")
        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        with self.db:
            self.db.execute(
                "UPDATE calls SET started_at = ? WHERE id = ?",
                (int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000), call.id),
            )
        update_call_transcript(self.db, call.id, SAMPLE_TRANSCRIPT)

        insert_transcript_chunk(
            self.db,
            call_id=call.id,
            person_id=person.id,
            content="Chunk without embedding",
            chunk_index=0,
            embedding=None,
        )
        fact = insert_fact(
            self.db,
            person_id=person.id,
            type="context",
            content="Fact without embedding",
            confidence=0.7,
            source="post-call",
            embedding=None,
        )

        async def fake_embed(_chunks: list[str]) -> list[list[float]]:
            return [make_embedding([0.9, 0.1])]

        with (
            patch("mystic.memory.run_nightly_extraction", new=AsyncMock(return_value=None)) as rerun,
            patch("mystic.memory.embed_chunks", new=AsyncMock(side_effect=fake_embed)),
        ):
            await run_retries(self.db)

        rerun.assert_awaited_once_with(self.db, "2026-03-11")
        chunk = get_chunks_by_call_id(self.db, call.id)[0]
        facts = get_all_active_facts_by_person(self.db, person.id)
        self.assertIsNotNone(chunk.embedding)
        self.assertEqual([entry.id for entry in facts], [fact.id])
        self.assertIsNotNone(facts[0].embedding)

    async def test_drain_retry_loop_waits_for_inflight_retry_cancellation(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_run_retries(_db: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch(
            "mystic.memory.run_retries",
            new=AsyncMock(side_effect=blocking_run_retries),
        ):
            start_retry_loop(self.db, interval_ms=1)
            await asyncio.wait_for(started.wait(), timeout=1)
            await drain_retry_loop(1000)
            await asyncio.wait_for(cancelled.wait(), timeout=1)

    async def test_successful_extraction_requeues_in_progress_action(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Carol")
        action = insert_action(self.db, person_id=person.id, intent="Follow up", source="agent")
        start_action_attempt(self.db, action.id)
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            action_id=action.id,
        )

        async def fake_execute(skill_name: str, *_args: object, **_kwargs: object) -> str:
            responses = {
                "summarize-call": '{"summary":"Follow-up call."}',
                "extract-facts": '{"facts":[]}',
                "extract-commitments": '{"commitments":[]}',
                "summarize-person": '{"summary":"Carol summary."}',
            }
            return responses[skill_name]

        with (
            patch("mystic.memory.execute_cognitive_skill", new=AsyncMock(side_effect=fake_execute)),
            patch("mystic.memory.check_satisfaction", new=AsyncMock(return_value=None)),
            patch("mystic.memory.embed_chunks", new=AsyncMock(return_value=[])),
        ):
            await run_extraction_pipeline(self.db, call.id, person.id, SAMPLE_TRANSCRIPT)

        updated_action = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(updated_action)
        assert updated_action is not None
        self.assertEqual(updated_action.status, "pending")
        self.assertIn("Call completed without fully resolving the action.", updated_action.result or "")
        self.assertIsNotNone(updated_action.due_at)


if __name__ == "__main__":
    unittest.main()
