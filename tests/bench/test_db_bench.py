"""Benchmarks for database operations — vec search, FTS, and pack/unpack."""

from __future__ import annotations

import sqlite3

import pytest

from mystic.db import pack_embedding
from mystic.memory import _run_fts_search, _run_vector_search, _run_vec0_search, TABLE_CONFIG

from .conftest import _random_embedding


@pytest.mark.bench
class TestPackEmbedding:
    def test_pack_list(self, benchmark):
        emb = _random_embedding()
        benchmark(pack_embedding, emb)

    def test_pack_bytes_passthrough(self, benchmark):
        emb_bytes = pack_embedding(_random_embedding())
        benchmark(pack_embedding, emb_bytes)


@pytest.mark.bench
class TestVecSearch:
    def test_vec0_search_200_rows(self, benchmark, populated_db: sqlite3.Connection):
        query_emb = _random_embedding()
        tc = TABLE_CONFIG["transcripts"]
        benchmark(
            _run_vec0_search,
            populated_db,
            tc,
            query_emb,
            person_id=None,
            oversample=20,
        )

    def test_vec_search_person_scoped(self, benchmark, populated_db: sqlite3.Connection):
        """Person-scoped search via production path (includes vec0 -> fallback)."""
        query_emb = _random_embedding()
        tc = TABLE_CONFIG["transcripts"]
        person = populated_db.execute("SELECT id FROM people LIMIT 1").fetchone()
        benchmark(
            _run_vector_search,
            populated_db,
            tc,
            query_emb,
            person_id=person["id"],
            oversample=20,
        )


@pytest.mark.bench
class TestFtsSearch:
    def test_fts_single_term(self, benchmark, populated_db: sqlite3.Connection):
        tc = TABLE_CONFIG["transcripts"]
        benchmark(
            _run_fts_search,
            populated_db,
            tc,
            "deliverables",
            person_id=None,
            oversample=20,
        )

    def test_fts_multi_term(self, benchmark, populated_db: sqlite3.Connection):
        tc = TABLE_CONFIG["transcripts"]
        benchmark(
            _run_fts_search,
            populated_db,
            tc,
            "project timeline budget",
            person_id=None,
            oversample=20,
        )

    def test_fts_person_scoped(self, benchmark, populated_db: sqlite3.Connection):
        tc = TABLE_CONFIG["transcripts"]
        person = populated_db.execute("SELECT id FROM people LIMIT 1").fetchone()
        benchmark(
            _run_fts_search,
            populated_db,
            tc,
            "meeting Tuesday",
            person_id=person["id"],
            oversample=20,
        )
