"""Fixtures for performance benchmarks."""

from __future__ import annotations

import math
import random
import sqlite3

import pytest

from mystic.db import (
    initialize_schema,
    insert_call,
    open_database,
    pack_embedding,
    replace_transcript_chunks_for_call,
    upsert_person,
    upsert_vec_row,
    get_rowid,
)
from tests.python_helpers import TempAppHome, seed_core_files, TEST_EMBEDDING_DIMENSIONS


def _random_embedding(dimensions: int = TEST_EMBEDDING_DIMENSIONS) -> list[float]:
    """Generate a unit-normalized random embedding vector."""
    raw = [random.gauss(0, 1) for _ in range(dimensions)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@pytest.fixture()
def bench_db():
    """In-memory SQLite with schema initialized — no mocking, real sqlite-vec."""
    with TempAppHome() as home:
        seed_core_files(home)
        db = open_database(":memory:")
        initialize_schema(db, dimensions=TEST_EMBEDDING_DIMENSIONS)
        yield db
        db.close()


@pytest.fixture()
def populated_db(bench_db: sqlite3.Connection):
    """DB seeded with 200 transcript chunks + embeddings across 5 people."""
    people = []
    for i in range(5):
        person = upsert_person(bench_db, f"+1555000{i:04d}", f"Person{i}")
        people.append(person)

    for person in people:
        for call_num in range(4):
            call = insert_call(
                bench_db,
                person_id=person.id,
                direction="inbound",
                audience="public",
            )
            chunks = []
            for chunk_idx in range(10):
                emb = _random_embedding()
                chunks.append({
                    "content": (
                        f"Conversation {call_num} chunk {chunk_idx} with {person.name}. "
                        f"Discussed project deliverables, timeline adjustments, and budget review. "
                        f"Meeting scheduled for next Tuesday afternoon to follow up on action items."
                    ),
                    "embedding": pack_embedding(emb),
                })
            replace_transcript_chunks_for_call(bench_db, call.id, person.id, chunks)

            # Insert vec rows for each chunk
            for chunk_idx in range(10):
                row = bench_db.execute(
                    "SELECT rowid FROM transcript_chunks WHERE call_id = ? AND chunk_index = ?",
                    (call.id, chunk_idx),
                ).fetchone()
                if row:
                    emb_bytes = pack_embedding(_random_embedding())
                    assert emb_bytes is not None
                    upsert_vec_row(
                        bench_db,
                        table="transcript_chunks_vec",
                        rowid_column="chunk_rowid",
                        rowid=int(row["rowid"]),
                        embedding=emb_bytes,
                    )
        bench_db.commit()

    yield bench_db
