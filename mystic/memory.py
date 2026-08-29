"""Flat memory module — chunking, embedding, retrieval, transcript indexing, FAQ, extraction, retry loop."""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from mystic.config import (
    emit_event,
    get_agent_config,
    get_error_message,
    get_home,
    get_intelligence_config,
    logger,
    read_soul,
    soul_exists,
)
from mystic.db import (
    bump_fact_confidence,
    clear_day_extraction_error,
    clear_extraction_error,
    delete_faq_chunks_by_file,
    delete_post_call_actions_by_call_id,
    delete_post_call_facts_by_call_id,
    get_action_by_id,
    get_actions_by_call_id,
    get_active_facts_by_person,
    get_all_active_facts_by_person,
    get_call_by_id,
    get_calls_needing_extraction,
    get_chunks_with_null_embeddings,
    get_day_summary,
    get_days_needing_extraction,
    get_facts_with_null_embeddings,
    get_person_by_id,
    get_people_with_interactions_on_date,
    get_recent_day_summaries,
    get_recent_summarized_calls_by_person,
    get_today_interactions,
    insert_action,
    insert_fact,
    mark_day_commitments_extracted,
    mark_day_extraction_complete,
    mark_day_extraction_error,
    mark_day_facts_extracted,
    mark_commitments_extracted,
    mark_extraction_attempted,
    mark_extraction_error,
    mark_facts_extracted,
    now_ms,
    pack_embedding,
    parse_due_at,
    replace_transcript_chunks_for_call,
    supersede_fact,
    update_day_summary,
    update_call_summary,
    update_chunk_embedding,
    update_fact_embedding,
    update_person_summary,
    upsert_day_summary,
    upsert_faq_chunk,
)
from mystic.types import (
    ActionUrgency,
    Audience,
    CallOriginContext,
    DaySummary,
    Direction,
    ExtractedCommitment,
    ExtractedFact,
    Fact,
    FactType,
    PersonContext,
    SearchResult,
    SelfContext,
)
from mystic.skills import execute_cognitive_skill
from mystic.actions import check_satisfaction, finalize_in_progress_action
from mystic.embedding import embed_chunks, embed_query
from mystic.interactions import describe_call
from mystic.llm import parse_json


# ── chunker ──────────────────────────────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 500
DEFAULT_OVERLAP = 100
SEPARATORS = ("\n\n", "\n", ". ", " ")
FACT_TYPES: set[FactType] = {"identity", "preference", "relationship", "context"}
ACTION_URGENCIES: set[ActionUrgency] = {"normal", "high"}


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")

    if len(text) <= chunk_size:
        trimmed = text.strip()
        return [trimmed] if trimmed else []

    return _recursive_split(text, SEPARATORS, chunk_size, overlap)


def _recursive_split(
    text: str,
    separators: tuple[str, ...],
    chunk_size: int,
    overlap: int,
) -> list[str]:
    if len(text) <= chunk_size:
        trimmed = text.strip()
        return [trimmed] if trimmed else []

    if not separators:
        return _hard_split(text, chunk_size, overlap)

    separator = separators[0]
    parts = text.split(separator)
    chunks: list[str] = []
    current = ""

    for part in parts:
        candidate = f"{current}{separator}{part}" if current else part
        if len(candidate) > chunk_size and current:
            chunks.append(current.strip())
            overlap_text = current[-overlap:] if overlap > 0 else ""
            current = f"{overlap_text}{separator}{part}" if overlap_text else part
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())

    remaining = separators[1:]
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) > chunk_size * 1.5 and remaining:
            result.extend(_recursive_split(chunk, remaining, chunk_size, overlap))
        else:
            result.append(chunk)
    return [chunk for chunk in result if chunk]


def _hard_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    step = max(chunk_size - overlap, 1)

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += step

    tail_start = max(len(text) - chunk_size, 0)
    tail = text[tail_start:].strip()
    if tail and (not chunks or tail != chunks[-1]):
        chunks.append(tail)

    return chunks


# ── retrieval ─────────────────────────────────────────────────────────────────

SearchTable = Literal["transcripts", "facts", "faq"]


@dataclass(frozen=True, slots=True)
class _TableConfig:
    source_table: str
    vec_table: str
    fts_table: str
    rowid_field: str
    person_filter: str | None = None


TABLE_CONFIG: dict[SearchTable, _TableConfig] = {
    "transcripts": _TableConfig(
        source_table="transcript_chunks",
        vec_table="transcript_chunks_vec",
        fts_table="transcript_chunks_fts",
        rowid_field="chunk_rowid",
        person_filter="person_id",
    ),
    "facts": _TableConfig(
        source_table="facts",
        vec_table="facts_vec",
        fts_table="facts_fts",
        rowid_field="fact_rowid",
        person_filter="person_id",
    ),
    "faq": _TableConfig(
        source_table="faq_chunks",
        vec_table="faq_vec",
        fts_table="faq_fts",
        rowid_field="chunk_rowid",
    ),
}


async def hybrid_search(
    db: sqlite3.Connection,
    table: SearchTable,
    query: str,
    person_id: str | None = None,
    limit: int | None = None,
) -> list[SearchResult]:
    config = get_intelligence_config().retrieval
    effective_limit = limit if limit is not None else config.limit
    if effective_limit <= 0:
        return []

    oversample = effective_limit * 4
    table_config = TABLE_CONFIG[table]

    query_embedding = await embed_query(query)
    fts_only = query_embedding is None
    if fts_only:
        logger.warn("retrieval.fts-only", table=table, reason="embedding failed")

    vec_results = (
        {}
        if query_embedding is None
        else _run_vector_search(
            db,
            table_config,
            query_embedding,
            person_id=person_id,
            oversample=oversample,
        )
    )
    fts_results = _run_fts_search(
        db,
        table_config,
        query,
        person_id=person_id,
        oversample=oversample,
    )

    fused: list[tuple[int, float]] = []
    for rowid in set(vec_results) | set(fts_results):
        if fts_only:
            score = fts_results.get(rowid, 0.0)
        else:
            score = (
                config.vectorWeight * vec_results.get(rowid, 0.0)
                + config.ftsWeight * fts_results.get(rowid, 0.0)
            )
        if score >= config.threshold:
            fused.append((rowid, score))

    fused.sort(key=lambda item: item[1], reverse=True)
    top_results = fused[:effective_limit]
    if not top_results:
        return []

    results = _load_source_rows(
        db,
        table,
        table_config,
        top_results,
        person_id=person_id,
    )
    logger.debug(
        "retrieval.complete",
        table=table,
        query=query[:50],
        results=len(results),
        ftsOnly=fts_only,
    )
    return results


def _run_vector_search(
    db: sqlite3.Connection,
    table_config: _TableConfig,
    query_embedding: list[float],
    *,
    person_id: str | None,
    oversample: int,
) -> dict[int, float]:
    if _uses_vec_fallback(db, table_config.vec_table):
        return _run_fallback_vector_search(
            db,
            table_config,
            query_embedding,
            person_id=person_id,
            oversample=oversample,
        )

    try:
        return _run_vec0_search(
            db,
            table_config,
            query_embedding,
            person_id=person_id,
            oversample=oversample,
        )
    except Exception as exc:
        logger.warn(
            "retrieval.vec.error",
            table=table_config.source_table,
            error=get_error_message(exc),
        )
        return _run_fallback_vector_search(
            db,
            table_config,
            query_embedding,
            person_id=person_id,
            oversample=oversample,
        )


def _run_vec0_search(
    db: sqlite3.Connection,
    table_config: _TableConfig,
    query_embedding: list[float],
    *,
    person_id: str | None,
    oversample: int,
) -> dict[int, float]:
    embedding_blob = pack_embedding(query_embedding)
    if embedding_blob is None:
        return {}

    if table_config.person_filter and person_id:
        rows = db.execute(
            f"""
            SELECT {table_config.vec_table}.{table_config.rowid_field} AS rowid,
                   {table_config.vec_table}.distance AS distance
            FROM {table_config.vec_table}
            JOIN {table_config.source_table}
              ON {table_config.source_table}.rowid = {table_config.vec_table}.{table_config.rowid_field}
            WHERE {table_config.vec_table}.embedding MATCH ?
              AND {table_config.source_table}.{table_config.person_filter} = ?
            ORDER BY {table_config.vec_table}.distance ASC
            LIMIT ?
            """,
            (embedding_blob, person_id, oversample),
        ).fetchall()
    else:
        rows = db.execute(
            f"""
            SELECT {table_config.vec_table}.{table_config.rowid_field} AS rowid,
                   {table_config.vec_table}.distance AS distance
            FROM {table_config.vec_table}
            JOIN {table_config.source_table}
              ON {table_config.source_table}.rowid = {table_config.vec_table}.{table_config.rowid_field}
            WHERE {table_config.vec_table}.embedding MATCH ?
            ORDER BY {table_config.vec_table}.distance ASC
            LIMIT ?
            """,
            (embedding_blob, oversample),
        ).fetchall()

    similarities = [(int(row["rowid"]), 1 - float(row["distance"])) for row in rows]
    return _normalize_ranked_scores(similarities)


def _run_fallback_vector_search(
    db: sqlite3.Connection,
    table_config: _TableConfig,
    query_embedding: list[float],
    *,
    person_id: str | None,
    oversample: int,
) -> dict[int, float]:
    if table_config.person_filter and person_id:
        rows = db.execute(
            f"""
            SELECT {table_config.vec_table}.{table_config.rowid_field} AS rowid,
                   {table_config.vec_table}.embedding AS embedding
            FROM {table_config.vec_table}
            JOIN {table_config.source_table}
              ON {table_config.source_table}.rowid = {table_config.vec_table}.{table_config.rowid_field}
            WHERE {table_config.source_table}.{table_config.person_filter} = ?
            """,
            (person_id,),
        ).fetchall()
    else:
        rows = db.execute(
            f"""
            SELECT {table_config.vec_table}.{table_config.rowid_field} AS rowid,
                   {table_config.vec_table}.embedding AS embedding
            FROM {table_config.vec_table}
            JOIN {table_config.source_table}
              ON {table_config.source_table}.rowid = {table_config.vec_table}.{table_config.rowid_field}
            """,
        ).fetchall()

    scored: list[tuple[int, float]] = []
    for row in rows:
        embedding = _unpack_embedding(row["embedding"])
        if not embedding:
            continue
        scored.append((int(row["rowid"]), _cosine_similarity(query_embedding, embedding)))

    scored.sort(key=lambda item: item[1], reverse=True)
    return _normalize_ranked_scores(scored[:oversample])


def _run_fts_search(
    db: sqlite3.Connection,
    table_config: _TableConfig,
    query: str,
    *,
    person_id: str | None,
    oversample: int,
) -> dict[int, float]:
    try:
        if table_config.person_filter and person_id:
            rows = db.execute(
                f"""
                SELECT {table_config.fts_table}.rowid AS rowid,
                       {table_config.fts_table}.rank AS rank
                FROM {table_config.fts_table}
                JOIN {table_config.source_table}
                  ON {table_config.source_table}.rowid = {table_config.fts_table}.rowid
                WHERE {table_config.fts_table} MATCH ?
                  AND {table_config.source_table}.{table_config.person_filter} = ?
                ORDER BY {table_config.fts_table}.rank ASC
                LIMIT ?
                """,
                (query, person_id, oversample),
            ).fetchall()
        else:
            rows = db.execute(
                f"""
                SELECT {table_config.fts_table}.rowid AS rowid,
                       {table_config.fts_table}.rank AS rank
                FROM {table_config.fts_table}
                JOIN {table_config.source_table}
                  ON {table_config.source_table}.rowid = {table_config.fts_table}.rowid
                WHERE {table_config.fts_table} MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (query, oversample),
            ).fetchall()
    except Exception as exc:
        logger.warn(
            "retrieval.fts.error",
            table=table_config.source_table,
            error=get_error_message(exc),
        )
        return {}

    ranked = [(int(row["rowid"]), abs(float(row["rank"]))) for row in rows]
    return _normalize_ranked_scores(ranked)


def _load_source_rows(
    db: sqlite3.Connection,
    table: SearchTable,
    table_config: _TableConfig,
    top_results: list[tuple[int, float]],
    *,
    person_id: str | None,
) -> list[SearchResult]:
    if not top_results:
        return []

    superseded_col = ", superseded_at" if table == "facts" else ""
    score_by_rowid = {rowid: score for rowid, score in top_results}
    rowids = list(score_by_rowid)
    placeholders = ", ".join("?" for _ in rowids)

    if table_config.person_filter and person_id:
        rows = db.execute(
            f"""
            SELECT {table_config.source_table}.rowid AS rowid,
                   {table_config.source_table}.id AS id,
                   {table_config.source_table}.content AS content,
                   {table_config.source_table}.{table_config.person_filter} AS person_id{superseded_col}
            FROM {table_config.source_table}
            WHERE {table_config.source_table}.rowid IN ({placeholders})
              AND {table_config.source_table}.{table_config.person_filter} = ?
            """,
            (*rowids, person_id),
        ).fetchall()
    else:
        rows = db.execute(
            f"""
            SELECT {table_config.source_table}.rowid AS rowid,
                   {table_config.source_table}.id AS id,
                   {table_config.source_table}.content AS content{superseded_col}
            FROM {table_config.source_table}
            WHERE {table_config.source_table}.rowid IN ({placeholders})
            """,
            rowids,
        ).fetchall()

    results: list[SearchResult] = []
    for row in rows:
        if table == "facts" and row["superseded_at"] is not None:
            continue
        results.append(
            SearchResult(
                id=str(row["id"]),
                content=str(row["content"]),
                score=score_by_rowid[int(row["rowid"])],
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _uses_vec_fallback(db: sqlite3.Connection, vec_table: str) -> bool:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (vec_table,),
    ).fetchone()
    sql = str(row["sql"]) if row is not None and row["sql"] is not None else ""
    sql_upper = sql.upper()
    return "VIRTUAL TABLE" not in sql_upper or "VEC0" not in sql_upper


def _normalize_ranked_scores(rows: list[tuple[int, float]]) -> dict[int, float]:
    if not rows:
        return {}

    max_score = rows[0][1]
    min_score = rows[-1][1]
    score_range = max_score - min_score or 1.0
    return {
        rowid: (score - min_score) / score_range
        for rowid, score in rows
    }


def _unpack_embedding(value: object) -> list[float]:
    if value is None:
        return []
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, bytearray):
        raw = bytes(value)
    elif isinstance(value, memoryview):
        raw = value.tobytes()
    else:
        raw = None
    if raw is None:
        return []
    unpacked = array("f")
    unpacked.frombytes(raw)
    return [float(item) for item in unpacked]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0

    length = min(len(left), len(right))
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for index in range(length):
        l_value = left[index]
        r_value = right[index]
        dot += l_value * r_value
        left_norm += l_value * l_value
        right_norm += r_value * r_value

    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


# ── transcript indexer ────────────────────────────────────────────────────────

async def index_transcript(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    transcript: str,
) -> int:
    if not transcript or not transcript.strip():
        return 0

    chunks = chunk_text(transcript)
    if not chunks:
        return 0

    embeddings = await embed_chunks(chunks)
    replace_transcript_chunks_for_call(
        db,
        call_id,
        person_id,
        [
            {
                "content": content,
                "embedding": embeddings[index] if embeddings is not None and index < len(embeddings) else None,
            }
            for index, content in enumerate(chunks)
        ],
    )

    logger.info(
        "transcript.indexed",
        callId=call_id,
        chunks=len(chunks),
        embeddingsOk=embeddings is not None,
    )
    return len(chunks)


async def _index_day_transcript(
    db: sqlite3.Connection,
    interactions: Sequence[object],
    person_id: str,
) -> int:
    total = 0
    for interaction in interactions:
        call_id = getattr(interaction, "id", None)
        transcript = getattr(interaction, "transcript", None)
        if not isinstance(call_id, str) or not isinstance(transcript, str):
            continue
        total += await index_transcript(db, call_id, person_id, transcript)
    return total


# ── faq indexer ───────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^#+\s+(.+)$", re.MULTILINE)


def get_faq_dir() -> Path:
    return get_home() / "faq"


async def index_faq_files(
    db: sqlite3.Connection,
    *,
    faq_dir: Path | None = None,
) -> int:
    directory = faq_dir or get_faq_dir()
    if not directory.exists():
        logger.debug("faq.indexer.skip", reason="no faq directory")
        return 0

    files = sorted(
        path for path in directory.iterdir() if path.is_file() and path.suffix == ".md"
    )
    total_chunks = 0

    for path in files:
        content = path.read_text(encoding="utf-8")
        delete_faq_chunks_by_file(db, path.name)

        chunks = chunk_text(content)
        if not chunks:
            continue

        embeddings = await embed_chunks(chunks)
        for index, chunk_content in enumerate(chunks):
            upsert_faq_chunk(
                db,
                chunk_id=f"faq-{path.name}-{index}",
                file_path=path.name,
                heading=_extract_heading(chunk_content),
                content=chunk_content,
                embedding=None if embeddings is None else embeddings[index],
            )

        total_chunks += len(chunks)
        logger.info("faq.indexed", file=path.name, chunks=len(chunks))

    logger.info("faq.indexer.complete", files=len(files), totalChunks=total_chunks)
    return total_chunks


async def search_faq(
    db: sqlite3.Connection,
    query: str,
    limit: int = 5,
) -> list[SearchResult]:
    return await hybrid_search(db, "faq", query, None, limit)


def _extract_heading(chunk_content: str) -> str | None:
    match = _HEADING_RE.search(chunk_content)
    if match is None:
        return None
    return match.group(1).strip() or None


# ── extraction ────────────────────────────────────────────────────────────────

PERSON_SUMMARY_FACT_LIMIT = 40
PERSON_SUMMARY_CALL_LIMIT = 20

_bootstrap_soul_fallback_in_flight: set[str] = set()
_extraction_in_flight: dict[str, asyncio.Task[None]] = {}


async def run_extraction_pipeline(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    transcript: str,
) -> None:
    existing = _extraction_in_flight.get(call_id)
    if existing is not None:
        logger.debug("extraction.in-flight", callId=call_id)
        await existing
        return

    task = asyncio.create_task(_run_extraction_pipeline_internal(db, call_id, person_id, transcript))
    _extraction_in_flight[call_id] = task
    try:
        await task
    finally:
        _extraction_in_flight.pop(call_id, None)


async def run_nightly_extraction(
    db: sqlite3.Connection,
    date: str,
) -> None:
    """Consolidate a day's interactions into long-term memory."""
    pending = get_days_needing_extraction(db, date)
    if not pending:
        logger.debug("nightly.extraction.skip", date=date, reason="no-pending-days")
        return

    logger.info("nightly.extraction.started", date=date, people=len(pending))
    for summary in pending:
        try:
            await _extract_day_for_person(db, summary.person_id, date, day_summary=summary)
        except Exception as exc:
            logger.error(
                "nightly.extraction.person.failed",
                personId=summary.person_id,
                date=date,
                error=get_error_message(exc),
            )
    logger.info("nightly.extraction.completed", date=date, people=len(pending))


async def rebuild_person_summary(
    db: sqlite3.Connection,
    person_id: str,
    person_name: str,
) -> None:
    facts = get_active_facts_by_person(db, person_id, PERSON_SUMMARY_FACT_LIMIT)
    fact_strings = [fact.content for fact in facts]
    recent_days = get_recent_day_summaries(db, person_id, PERSON_SUMMARY_CALL_LIMIT)
    call_summaries = [f"{day.date}: {day.summary}" for day in recent_days if day.summary]
    if not call_summaries:
        calls = get_recent_summarized_calls_by_person(db, person_id, PERSON_SUMMARY_CALL_LIMIT)
        call_summaries = [
            f"{_format_date(call.started_at)}: {call.summary}"
            for call in calls
            if call.summary
        ]
    raw = await execute_cognitive_skill(
        "summarize-person",
        SelfContext(
            person=PersonContext(name=person_name, summary=None, facts=fact_strings),
            recent_calls=call_summaries,
        ),
        "Generate the person summary.",
    )
    parsed = parse_json(raw)
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if isinstance(summary, str) and summary.strip():
        update_person_summary(db, person_id, summary)


async def _extract_day_for_person(
    db: sqlite3.Connection,
    person_id: str,
    date: str,
    *,
    day_summary: DaySummary | None = None,
) -> None:
    person = get_person_by_id(db, person_id)
    if person is None:
        return

    interactions = [
        call
        for call in get_today_interactions(db, person_id, date)
        if (call.transcript or "").strip()
    ]
    if not interactions:
        return

    merged_transcript = _merge_day_transcripts(interactions)
    if not merged_transcript.strip():
        return

    stored_day_summary = day_summary or upsert_day_summary(db, person_id, date)

    person_name = person.name or "the caller"
    existing_facts = get_all_active_facts_by_person(db, person_id)
    base_context = SelfContext(
        person=PersonContext(
            name=person_name,
            summary=person.summary,
            facts=[f"{fact.content} ({fact.type}, confidence: {fact.confidence})" for fact in existing_facts],
        ),
    )
    representative_call_id = interactions[-1].id

    results = await asyncio.gather(
        _index_day_transcript(db, interactions, person_id),
        _extract_day_summary(db, stored_day_summary.id, base_context, merged_transcript),
        _extract_day_facts(
            db,
            stored_day_summary.id,
            representative_call_id,
            person_id,
            base_context,
            existing_facts,
            merged_transcript,
        )
        if stored_day_summary.facts_extracted == 0
        else _noop(),
        _extract_day_commitments(
            db,
            stored_day_summary.id,
            representative_call_id,
            person_id,
            base_context,
            merged_transcript,
        )
        if stored_day_summary.commitments_extracted == 0
        else _noop(),
        return_exceptions=True,
    )

    extraction_errors = _collect_phase_errors(
        "daySummaryId",
        stored_day_summary.id,
        ("transcript", "summary", "facts", "commitments"),
        results,
        event_prefix="nightly.extraction",
    )
    if extraction_errors:
        mark_day_extraction_error(db, stored_day_summary.id, " | ".join(extraction_errors))
    else:
        clear_day_extraction_error(db, stored_day_summary.id)

    try:
        await rebuild_person_summary(db, person_id, person_name)
    except Exception as exc:
        logger.error(
            "nightly.extraction.person-summary.failed",
            daySummaryId=stored_day_summary.id,
            error=get_error_message(exc),
        )

    if not extraction_errors:
        mark_day_extraction_complete(db, stored_day_summary.id)


async def _run_extraction_pipeline_internal(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    transcript: str,
) -> None:
    call = get_call_by_id(db, call_id)
    if call is None:
        return

    person = get_person_by_id(db, person_id)
    person_name = person.name if person and person.name else "the caller"
    existing_facts = get_all_active_facts_by_person(db, person_id)
    base_context = SelfContext(
        person=PersonContext(
            name=person_name,
            summary=person.summary if person else None,
            facts=[f"{fact.content} ({fact.type}, confidence: {fact.confidence})" for fact in existing_facts],
        ),
        call_origin=CallOriginContext(
            direction=call.direction,
            audience=call.audience,
            channel=call.channel,
            modality=call.modality,
        ),
    )

    logger.info(
        "extraction.started",
        callId=call_id,
        tracks=["transcript", "summary", "facts", "commitments"],
    )

    phase1_results = await asyncio.gather(
        index_transcript(db, call_id, person_id, transcript),
        _extract_call_summary(db, call_id, base_context, transcript),
        _extract_facts(db, call_id, person_id, base_context, existing_facts, transcript)
        if call.facts_extracted == 0
        else _noop(),
        _extract_commitments(db, call_id, person_id, base_context, transcript)
        if call.commitments_extracted == 0
        else _noop(),
        return_exceptions=True,
    )

    extraction_errors = _collect_phase1_errors(call_id, phase1_results)
    if extraction_errors:
        mark_extraction_error(db, call_id, " | ".join(extraction_errors))
    else:
        clear_extraction_error(db, call_id)

    await _maybe_write_bootstrap_soul_from_transcript(db, call_id, base_context, transcript)

    try:
        await rebuild_person_summary(db, person_id, person_name)
    except Exception as exc:
        logger.error("extraction.person-summary.failed", callId=call_id, error=get_error_message(exc))

    try:
        await check_satisfaction(db, call_id, person_id)
    except Exception as exc:
        logger.error("extraction.satisfaction.failed", callId=call_id, error=get_error_message(exc))

    if call.action_id:
        finalize_in_progress_action(
            db,
            call.action_id,
            "Call completed without fully resolving the action.",
        )
    logger.info("extraction.completed", callId=call_id)


async def _extract_call_summary(
    db: sqlite3.Connection,
    call_id: str,
    self_context: SelfContext,
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("summarize-call", self_context, transcript)
    parsed = parse_json(raw)
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if isinstance(summary, str) and summary.strip():
        update_call_summary(db, call_id, summary)


async def _extract_day_summary(
    db: sqlite3.Connection,
    day_summary_id: str,
    self_context: SelfContext,
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("summarize-call", self_context, transcript)
    parsed = parse_json(raw)
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if isinstance(summary, str) and summary.strip():
        update_day_summary(db, day_summary_id, summary)


async def _extract_facts(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    self_context: SelfContext,
    existing_facts: Sequence[Fact],
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("extract-facts", self_context, transcript)
    parsed = parse_json(raw)
    facts_raw = parsed.get("facts") if isinstance(parsed, dict) else None
    facts = [_coerce_fact(item) for item in facts_raw] if isinstance(facts_raw, list) else []
    facts = [fact for fact in facts if fact is not None]

    fact_contents = [fact.content for fact in facts]
    embeddings = await embed_chunks(fact_contents) if fact_contents else []
    embeddings = embeddings or []
    delete_post_call_facts_by_call_id(db, call_id)
    comparable_facts = [
        fact
        for fact in existing_facts
        if not (fact.call_id == call_id and fact.source == "post-call")
    ]

    for index, fact in enumerate(facts):
        embedding = embeddings[index] if index < len(embeddings) else None
        match = next(
            (
                existing
                for existing in comparable_facts
                if _fact_matches(existing.content, fact.content)
            ),
            None,
        )
        if match is not None:
            if fact.confidence > match.confidence:
                supersede_fact(db, match.id)
                insert_fact(
                    db,
                    person_id=person_id,
                    call_id=call_id,
                    source_text=fact.source_text,
                    type=fact.type,
                    content=fact.content,
                    confidence=fact.confidence,
                    source="post-call",
                    embedding=embedding,
                )
            else:
                bump_fact_confidence(db, match.id, fact.confidence)
            continue

        insert_fact(
            db,
            person_id=person_id,
            call_id=call_id,
            source_text=fact.source_text,
            type=fact.type,
            content=fact.content,
            confidence=fact.confidence,
            source="post-call",
            embedding=embedding,
        )

    mark_facts_extracted(db, call_id)
    if facts:
        emit_event("activity", {
            "type": "facts_extracted",
            "call_id": call_id,
            "person_id": person_id,
            "count": len(facts),
        })
    logger.info("extraction.facts.done", callId=call_id, count=len(facts))


async def _extract_day_facts(
    db: sqlite3.Connection,
    day_summary_id: str,
    representative_call_id: str,
    person_id: str,
    self_context: SelfContext,
    existing_facts: Sequence[Fact],
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("extract-facts", self_context, transcript)
    parsed = parse_json(raw)
    facts_raw = parsed.get("facts") if isinstance(parsed, dict) else None
    facts = [_coerce_fact(item) for item in facts_raw] if isinstance(facts_raw, list) else []
    facts = [fact for fact in facts if fact is not None]

    fact_contents = [fact.content for fact in facts]
    embeddings = await embed_chunks(fact_contents) if fact_contents else []
    embeddings = embeddings or []
    delete_post_call_facts_by_call_id(db, representative_call_id)
    comparable_facts = [
        fact
        for fact in existing_facts
        if not (fact.call_id == representative_call_id and fact.source == "post-call")
    ]

    for index, fact in enumerate(facts):
        embedding = embeddings[index] if index < len(embeddings) else None
        match = next(
            (
                existing
                for existing in comparable_facts
                if _fact_matches(existing.content, fact.content)
            ),
            None,
        )
        if match is not None:
            if fact.confidence > match.confidence:
                supersede_fact(db, match.id)
                insert_fact(
                    db,
                    person_id=person_id,
                    call_id=representative_call_id,
                    source_text=fact.source_text,
                    type=fact.type,
                    content=fact.content,
                    confidence=fact.confidence,
                    source="post-call",
                    embedding=embedding,
                )
            else:
                bump_fact_confidence(db, match.id, fact.confidence)
            continue

        insert_fact(
            db,
            person_id=person_id,
            call_id=representative_call_id,
            source_text=fact.source_text,
            type=fact.type,
            content=fact.content,
            confidence=fact.confidence,
            source="post-call",
            embedding=embedding,
        )

    mark_day_facts_extracted(db, day_summary_id)
    if facts:
        emit_event("activity", {
            "type": "facts_extracted",
            "call_id": representative_call_id,
            "person_id": person_id,
            "count": len(facts),
        })
    logger.info(
        "nightly.extraction.facts.done",
        daySummaryId=day_summary_id,
        representativeCallId=representative_call_id,
        count=len(facts),
    )


async def _extract_commitments(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    self_context: SelfContext,
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("extract-commitments", self_context, transcript)
    parsed = parse_json(raw)
    commitments_raw = parsed.get("commitments") if isinstance(parsed, dict) else None
    commitments = (
        [_coerce_commitment(item) for item in commitments_raw]
        if isinstance(commitments_raw, list)
        else []
    )
    commitments = [commitment for commitment in commitments if commitment is not None]

    mid_call_actions = [action for action in get_actions_by_call_id(db, call_id) if action.source != "post-call"]
    delete_post_call_actions_by_call_id(db, call_id)

    for commitment in commitments:
        if any(_action_matches(action.intent, commitment.intent) for action in mid_call_actions):
            continue
        insert_action(
            db,
            person_id=person_id,
            call_id=call_id,
            source_text=commitment.content,
            intent=commitment.intent,
            due_at=parse_due_at(commitment.due),
            urgency=commitment.urgency,
            source="post-call",
        )

    mark_commitments_extracted(db, call_id)
    if commitments:
        emit_event("activity", {
            "type": "actions_extracted",
            "call_id": call_id,
            "person_id": person_id,
            "count": len(commitments),
        })
    logger.info("extraction.commitments.done", callId=call_id, count=len(commitments))


async def _extract_day_commitments(
    db: sqlite3.Connection,
    day_summary_id: str,
    representative_call_id: str,
    person_id: str,
    self_context: SelfContext,
    transcript: str,
) -> None:
    raw = await execute_cognitive_skill("extract-commitments", self_context, transcript)
    parsed = parse_json(raw)
    commitments_raw = parsed.get("commitments") if isinstance(parsed, dict) else None
    commitments = (
        [_coerce_commitment(item) for item in commitments_raw]
        if isinstance(commitments_raw, list)
        else []
    )
    commitments = [commitment for commitment in commitments if commitment is not None]

    mid_call_actions = [
        action
        for action in get_actions_by_call_id(db, representative_call_id)
        if action.source != "post-call"
    ]
    delete_post_call_actions_by_call_id(db, representative_call_id)

    for commitment in commitments:
        if any(_action_matches(action.intent, commitment.intent) for action in mid_call_actions):
            continue
        insert_action(
            db,
            person_id=person_id,
            call_id=representative_call_id,
            source_text=commitment.content,
            intent=commitment.intent,
            due_at=parse_due_at(commitment.due),
            urgency=commitment.urgency,
            source="post-call",
        )

    mark_day_commitments_extracted(db, day_summary_id)
    if commitments:
        emit_event("activity", {
            "type": "actions_extracted",
            "call_id": representative_call_id,
            "person_id": person_id,
            "count": len(commitments),
        })
    logger.info(
        "nightly.extraction.commitments.done",
        daySummaryId=day_summary_id,
        representativeCallId=representative_call_id,
        count=len(commitments),
    )


async def _maybe_write_bootstrap_soul_from_transcript(
    db: sqlite3.Connection,
    call_id: str,
    base_self_context: SelfContext,
    transcript: str,
) -> None:
    call = get_call_by_id(db, call_id)
    if call is None or not transcript.strip():
        return
    if not _is_bootstrap_call(db, call.action_id, call.audience, call.direction):
        return
    if not _should_generate_bootstrap_soul_fallback():
        return
    if call.id in _bootstrap_soul_fallback_in_flight:
        return

    _bootstrap_soul_fallback_in_flight.add(call.id)
    try:
        instruction = "\n".join(
            (
                "Bootstrap fallback: write a complete SOUL.md from this transcript.",
                "Use first person voice and markdown.",
                "Capture values, tone, boundaries, and caller-handling expectations.",
                "Return only the full SOUL.md content.",
                "",
                "Transcript:",
                transcript,
            )
        )
        await execute_cognitive_skill("edit-soul", base_self_context, instruction)
        logger.info("extraction.bootstrap.soul-fallback.written", callId=call.id)
    except Exception as exc:
        logger.error(
            "extraction.bootstrap.soul-fallback.failed",
            callId=call.id,
            error=get_error_message(exc),
        )
    finally:
        _bootstrap_soul_fallback_in_flight.discard(call.id)


def _is_bootstrap_call(
    db: sqlite3.Connection,
    action_id: str | None,
    audience: Audience,
    direction: Direction,
) -> bool:
    if audience != "owner" or direction != "outbound" or not action_id:
        return False
    action = get_action_by_id(db, action_id)
    if action is None:
        return False
    context = (action.context or "").lower()
    intent = action.intent.lower()
    return context.startswith("bootstrap:") or "bootstrap" in intent or intent == "get to know owner"


def _should_generate_bootstrap_soul_fallback() -> bool:
    if not soul_exists():
        return True
    try:
        return not read_soul().strip()
    except OSError:
        return True


def _collect_phase_errors(
    context_key: str,
    context_value: str,
    labels: Sequence[str],
    results: Sequence[object],
    *,
    event_prefix: str = "extraction",
) -> list[str]:
    errors: list[str] = []
    for label, result in zip(labels, results, strict=True):
        if isinstance(result, Exception):
            errors.append(f"{label}: {get_error_message(result)}")
            logger.error(
                f"{event_prefix}.{label}.failed",
                **{context_key: context_value},
                error=get_error_message(result),
            )
    return errors


def _collect_phase1_errors(call_id: str, results: Sequence[object]) -> list[str]:
    return _collect_phase_errors(
        "callId",
        call_id,
        ("transcript", "summary", "facts", "commitments"),
        results,
    )


def _agent_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(get_agent_config().hours.timezone)
    except Exception:
        return ZoneInfo("UTC")


def _coerce_fact(item: object) -> ExtractedFact | None:
    if not isinstance(item, dict):
        return None
    content = item.get("content")
    type_name = item.get("type")
    confidence = item.get("confidence")
    source_text = item.get("source_text")
    if not isinstance(content, str) or not isinstance(type_name, str) or type_name not in FACT_TYPES:
        return None
    return ExtractedFact(
        content=content,
        type=cast(FactType, type_name),
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.5,
        source_text=source_text if isinstance(source_text, str) else "",
    )


def _coerce_commitment(item: object) -> ExtractedCommitment | None:
    if not isinstance(item, dict):
        return None
    content = item.get("content")
    intent = item.get("intent")
    urgency = item.get("urgency")
    if (
        not isinstance(content, str)
        or not isinstance(intent, str)
        or not isinstance(urgency, str)
        or urgency not in ACTION_URGENCIES
    ):
        return None
    due = item.get("due")
    return ExtractedCommitment(
        content=content,
        intent=intent,
        due=due if isinstance(due, str) else None,
        urgency=cast(ActionUrgency, urgency),
    )


def _fact_matches(existing: str, candidate: str) -> bool:
    existing_key = existing.lower()[:20]
    candidate_key = candidate.lower()[:20]
    return existing_key in candidate.lower() or candidate_key in existing.lower()


def _action_matches(existing_intent: str, candidate_intent: str) -> bool:
    return candidate_intent.lower()[:20] in existing_intent.lower()


def _merge_day_transcripts(interactions: Sequence[object]) -> str:
    tz = _agent_timezone()
    parts: list[str] = []
    for interaction in interactions:
        transcript = str(getattr(interaction, "transcript", "") or "").strip()
        if not transcript:
            continue
        started_at = getattr(interaction, "started_at", None)
        descriptor = describe_call(interaction)
        heading = descriptor.channel_label
        if isinstance(started_at, int):
            started = datetime.fromtimestamp(started_at / 1000, tz)
            heading = f"{started.strftime('%Y-%m-%d %I:%M %p')} {descriptor.channel_label}"
        parts.append(f"[interaction {heading}]\n{transcript}")
    return "\n\n".join(parts)


def _format_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%m/%d/%Y")


async def _noop() -> None:
    return None


# ── nightly extraction and compatibility loops ───────────────────────────────

NIGHTLY_HOUR = 2
RETRY_INTERVAL_MS = 5 * 60 * 1000

_nightly_task: asyncio.Task[None] | None = None
_retry_task: asyncio.Task[None] | None = None


def start_nightly_loop(db: sqlite3.Connection) -> None:
    global _nightly_task
    if _nightly_task is not None and not _nightly_task.done():
        return
    _nightly_task = asyncio.create_task(_nightly_loop(db), name="nightly-extraction")
    logger.info("nightly.loop.started", hour=NIGHTLY_HOUR)


async def drain_nightly_loop(timeout_ms: int = 0) -> None:
    global _nightly_task
    task = _nightly_task
    _nightly_task = None
    if task is None:
        return
    if not task.done():
        task.cancel()
        logger.info("nightly.loop.stopped")
    try:
        if timeout_ms > 0:
            await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        else:
            await task
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warn("nightly.loop.stop.timeout", timeoutMs=timeout_ms)
    except Exception as exc:
        logger.warn("nightly.loop.stop.error", error=get_error_message(exc))


async def _nightly_loop(db: sqlite3.Connection) -> None:
    try:
        await _run_missed_nightly_extractions(db)
        await run_embedding_backfill(db)
        while True:
            await asyncio.sleep(_seconds_until_next_nightly_run())
            tz = _agent_timezone()
            yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            await run_nightly_extraction(db, yesterday)
            await run_embedding_backfill(db)
    except asyncio.CancelledError:
        raise


def _seconds_until_next_nightly_run() -> float:
    tz = _agent_timezone()
    now = datetime.now(tz)
    target = now.replace(hour=NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


async def _run_missed_nightly_extractions(db: sqlite3.Connection) -> None:
    today = datetime.now(_agent_timezone()).strftime("%Y-%m-%d")
    for date in _get_interaction_dates(db):
        if date >= today:
            continue
        if not get_days_needing_extraction(db, date):
            continue
        logger.info("nightly.extraction.catchup", date=date)
        await run_nightly_extraction(db, date)


def _get_interaction_dates(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        """
        SELECT started_at
        FROM calls
        WHERE transcript IS NOT NULL
        ORDER BY started_at ASC
        """
    ).fetchall()
    seen: set[str] = set()
    dates: list[str] = []
    for row in rows:
        started_at = row["started_at"]
        if not isinstance(started_at, int):
            continue
        date = datetime.fromtimestamp(started_at / 1000, _agent_timezone()).strftime("%Y-%m-%d")
        if date in seen:
            continue
        seen.add(date)
        dates.append(date)
    return dates


async def run_embedding_backfill(db: sqlite3.Connection) -> None:
    null_chunks = get_chunks_with_null_embeddings(db)
    if null_chunks:
        logger.info("retry.embeddings.chunks", count=len(null_chunks))
        for chunk in null_chunks:
            try:
                embeddings = await embed_chunks([chunk.content])
                if embeddings:
                    update_chunk_embedding(db, chunk.id, embeddings[0])
            except Exception as exc:
                logger.warn("retry.embed.chunk.failed", chunkId=chunk.id, error=get_error_message(exc))

    null_facts = get_facts_with_null_embeddings(db)
    if null_facts:
        logger.info("retry.embeddings.facts", count=len(null_facts))
        for fact in null_facts:
            try:
                embeddings = await embed_chunks([fact.content])
                if embeddings:
                    update_fact_embedding(db, fact.id, embeddings[0])
            except Exception as exc:
                logger.warn("retry.embed.fact.failed", factId=fact.id, error=get_error_message(exc))


async def run_retries(db: sqlite3.Connection) -> None:
    """Compatibility helper for tests and manual catch-up."""
    for date in _get_interaction_dates(db):
        if get_days_needing_extraction(db, date):
            await run_nightly_extraction(db, date)
    await run_embedding_backfill(db)


def start_retry_loop(
    db: sqlite3.Connection,
    *,
    interval_ms: int = RETRY_INTERVAL_MS,
) -> None:
    global _retry_task
    if _retry_task is not None and not _retry_task.done():
        return
    _retry_task = asyncio.create_task(_retry_loop(db, interval_ms), name="memory-retry-compat")
    logger.info("retry.loop.started", intervalMs=interval_ms)


def stop_retry_loop() -> None:
    global _retry_task
    task = _retry_task
    _retry_task = None
    if task is None:
        return
    if not task.done():
        task.cancel()
    logger.info("retry.loop.stopped")


async def drain_retry_loop(timeout_ms: int = 0) -> None:
    global _retry_task
    task = _retry_task
    _retry_task = None
    if task is None:
        return
    if not task.done():
        task.cancel()
        logger.info("retry.loop.stopped")
    try:
        if timeout_ms > 0:
            await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        else:
            await task
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warn("retry.loop.stop.timeout", timeoutMs=timeout_ms)
    except Exception as exc:
        logger.warn("retry.loop.stop.error", error=get_error_message(exc))


async def _retry_loop(db: sqlite3.Connection, interval_ms: int) -> None:
    try:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await run_retries(db)
    except asyncio.CancelledError:
        raise
