"""Benchmarks for the composed retrieval path via the current hybrid-search API."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from mystic.db import insert_call, insert_fact, upsert_person
from mystic.memory import hybrid_search, index_transcript
from tests.python_helpers import make_embedding


@dataclass(slots=True)
class RetrievalBenchContext:
    db: sqlite3.Connection
    person_id: str
    transcript_query: str
    fact_query: str
    query_embedding: list[float]


def _run_async(
    loop: asyncio.AbstractEventLoop,
    async_fn: Any,
    *args: object,
) -> object:
    return loop.run_until_complete(async_fn(*args))


async def _fake_embed_chunks(chunks: list[str]) -> list[list[float]]:
    return [
        make_embedding([1.0, float(index + 1), float(len(chunk)) / 100.0])
        for index, chunk in enumerate(chunks)
    ]


@pytest.fixture()
def retrieval_context(bench_db: sqlite3.Connection) -> RetrievalBenchContext:
    primary = upsert_person(bench_db, "+15550010001", "Casey")
    secondary = upsert_person(bench_db, "+15550010002", "Morgan")

    primary_call = insert_call(
        bench_db,
        person_id=primary.id,
        direction="inbound",
        audience="public",
    )
    secondary_call = insert_call(
        bench_db,
        person_id=secondary.id,
        direction="inbound",
        audience="public",
    )

    primary_transcript = (
        "Casey reviewed the budget report and asked to keep the Tuesday afternoon "
        "follow-up on the calendar with deliverables ready for discussion."
    )
    secondary_transcript = (
        "Morgan asked to move the Wednesday morning standup after discussing travel "
        "logistics and vendor onboarding."
    )

    loop = asyncio.new_event_loop()
    try:
        with patch("mystic.memory.embed_chunks", new=_fake_embed_chunks):
            loop.run_until_complete(index_transcript(bench_db, primary_call.id, primary.id, primary_transcript))
            loop.run_until_complete(index_transcript(bench_db, secondary_call.id, secondary.id, secondary_transcript))
    finally:
        loop.close()

    fact_texts = (
        "Prefers Tuesday afternoon follow-ups for budget review.",
        "Needs the quarterly deliverables summary before the next call.",
        "Asked for travel updates before vendor onboarding.",
    )
    fact_people = (primary.id, primary.id, secondary.id)
    for index, (person_id, content) in enumerate(zip(fact_people, fact_texts, strict=True)):
        insert_fact(
            bench_db,
            person_id=person_id,
            type="context",
            content=content,
            confidence=0.9,
            source="owner",
            embedding=make_embedding([1.0, float(index + 1), 0.25]),
        )

    return RetrievalBenchContext(
        db=bench_db,
        person_id=primary.id,
        transcript_query="budget review Tuesday afternoon deliverables",
        fact_query="Tuesday afternoon budget follow-up",
        query_embedding=make_embedding([1.0, 1.0, 0.25]),
    )


@pytest.mark.bench
class TestHybridSearchBench:
    def test_transcripts_end_to_end(self, benchmark, retrieval_context: RetrievalBenchContext) -> None:
        async def fake_embed_query(_query: str) -> list[float]:
            return retrieval_context.query_embedding

        loop = asyncio.new_event_loop()
        try:
            with patch("mystic.memory.embed_query", new=fake_embed_query):
                benchmark(
                    _run_async,
                    loop,
                    hybrid_search,
                    retrieval_context.db,
                    "transcripts",
                    retrieval_context.transcript_query,
                    retrieval_context.person_id,
                    5,
                )
        finally:
            loop.close()

    def test_facts_end_to_end(self, benchmark, retrieval_context: RetrievalBenchContext) -> None:
        async def fake_embed_query(_query: str) -> list[float]:
            return retrieval_context.query_embedding

        loop = asyncio.new_event_loop()
        try:
            with patch("mystic.memory.embed_query", new=fake_embed_query):
                benchmark(
                    _run_async,
                    loop,
                    hybrid_search,
                    retrieval_context.db,
                    "facts",
                    retrieval_context.fact_query,
                    retrieval_context.person_id,
                    5,
                )
        finally:
            loop.close()
