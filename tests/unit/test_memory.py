from __future__ import annotations

import json
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch

from mystic.config import clear_config_cache
from mystic.db import insert_call, close_database, initialize_schema, open_database, get_active_facts_by_person, insert_fact, supersede_fact, upsert_person, get_chunks_by_call_id, replace_transcript_chunks_for_call, upsert_faq_chunk
from mystic.memory import chunk_text, embed_chunks, embed_query, hybrid_search, index_transcript
from tests.python_helpers import TempAppHome, TEST_PROVIDERS_CONFIG, seed_core_files, make_embedding


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        clear_config_cache()
        self.db = open_database(":memory:")
        initialize_schema(self.db)

    def tearDown(self) -> None:
        close_database(self.db)
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    def _set_embedding_provider(self, embedding_payload: dict[str, object]) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["embedding"] = embedding_payload
        seed_core_files(self.home, providers=providers)
        clear_config_cache("providers.json")

    async def test_embed_chunks_local_provider_dispatches_to_sync_embedder(self) -> None:
        self._set_embedding_provider(
            {"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256}
        )
        fake_loop = AsyncMock()
        fake_loop.run_in_executor = AsyncMock(return_value=[[0.6, 0.8]])
        with (
            patch(
                "mystic.embedding.get_local_model_dir",
                return_value=self.home / "models" / "nomic",
            ),
            patch("mystic.embedding.asyncio.get_running_loop", return_value=fake_loop),
        ):
            result = await embed_chunks(["chunk-a"])

        self.assertEqual(result, [[0.6, 0.8]])
        fake_loop.run_in_executor.assert_awaited_once()

    async def test_embed_query_local_provider_dispatches_to_sync_embedder(self) -> None:
        self._set_embedding_provider(
            {"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256}
        )
        fake_loop = AsyncMock()
        fake_loop.run_in_executor = AsyncMock(return_value=[[0.9, 0.1]])
        with (
            patch(
                "mystic.embedding.get_local_model_dir",
                return_value=self.home / "models" / "nomic",
            ),
            patch("mystic.embedding.asyncio.get_running_loop", return_value=fake_loop),
        ):
            result = await embed_query("hello world")

        self.assertEqual(result, [0.9, 0.1])
        fake_loop.run_in_executor.assert_awaited_once()

    def test_chunk_text_splits_large_input(self) -> None:
        text = (
            "Paragraph one keeps going with enough words to force a split. " * 8
            + "\n\n"
            + "Paragraph two adds even more text so the recursive separator logic runs. " * 8
        )
        chunks = chunk_text(text, chunk_size=120, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertTrue(any("Paragraph two" in chunk for chunk in chunks))

    async def test_hybrid_search_scopes_transcripts_with_fts_only_fallback(self) -> None:
        alice = upsert_person(self.db, "+15550001111", "Alice")
        bob = upsert_person(self.db, "+15550002222", "Bob")
        alice_call = insert_call(self.db, person_id=alice.id, direction="inbound", audience="public")
        bob_call = insert_call(self.db, person_id=bob.id, direction="inbound", audience="public")

        replace_transcript_chunks_for_call(
            self.db,
            alice_call.id,
            alice.id,
            [{"content": "Alice scheduled the Tuesday meeting."}],
        )
        replace_transcript_chunks_for_call(
            self.db,
            bob_call.id,
            bob.id,
            [{"content": "Bob scheduled the Wednesday meeting."}],
        )

        with patch("mystic.memory.embed_query", new=AsyncMock(return_value=None)):
            results = await hybrid_search(
                self.db,
                "transcripts",
                "scheduled meeting",
                person_id=alice.id,
                limit=5,
            )

        self.assertEqual(len(results), 1)
        self.assertIn("Alice", results[0].content)

    async def test_hybrid_search_filters_superseded_facts(self) -> None:
        person = upsert_person(self.db, "+15550003333", "Eve")
        active = insert_fact(
            self.db,
            person_id=person.id,
            type="context",
            content="Eve prefers email follow-ups.",
            confidence=0.9,
            source="post-call",
        )
        stale = insert_fact(
            self.db,
            person_id=person.id,
            type="context",
            content="Eve prefers voicemail.",
            confidence=0.7,
            source="post-call",
        )
        supersede_fact(self.db, stale.id)

        with patch("mystic.memory.embed_query", new=AsyncMock(return_value=None)):
            results = await hybrid_search(
                self.db,
                "facts",
                "Eve prefers",
                person_id=person.id,
                limit=10,
            )

        self.assertEqual([result.id for result in results], [active.id])
        self.assertEqual([fact.id for fact in get_active_facts_by_person(self.db, person.id)], [active.id])

    async def test_hybrid_search_uses_vector_ranking_when_available(self) -> None:
        upsert_faq_chunk(
            self.db,
            chunk_id="faq-alpha",
            file_path="faq.md",
            content="alpha content",
            embedding=make_embedding([1.0, 0.0]),
        )
        upsert_faq_chunk(
            self.db,
            chunk_id="faq-beta",
            file_path="faq.md",
            content="beta content",
            embedding=make_embedding([0.0, 1.0]),
        )

        with patch(
            "mystic.memory.embed_query",
            new=AsyncMock(return_value=make_embedding([1.0, 0.0])),
        ):
            results = await hybrid_search(self.db, "faq", "unmatched-query", limit=2)

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0].id, "faq-alpha")

    async def test_index_transcript_stores_chunks_and_embeddings(self) -> None:
        person = upsert_person(self.db, "+15550004444", "Dana")
        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        transcript = ("Dana asked for a follow-up next Tuesday. " * 20).strip()

        async def fake_embed(chunks: list[str]) -> list[list[float]]:
            return [
                make_embedding([float(index), float(index + 1)])
                for index, _ in enumerate(chunks)
            ]

        with patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(side_effect=fake_embed),
        ):
            chunk_count = await index_transcript(self.db, call.id, person.id, transcript)

        stored = get_chunks_by_call_id(self.db, call.id)
        self.assertEqual(len(stored), chunk_count)
        self.assertGreater(chunk_count, 1)
        self.assertTrue(all(chunk.embedding is not None for chunk in stored))


if __name__ == "__main__":
    unittest.main()
