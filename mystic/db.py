"""Flat database layer for Mystic Horizon — all DB helpers in one module."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from array import array
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypeVar, cast
from zoneinfo import ZoneInfo

from mystic.config import get_agent_config, get_embedding_dimensions, get_error_message, get_home, logger
from mystic.types import (
    Action,
    ActionSource,
    ActionStatus,
    ActionUrgency,
    Audience,
    Call,
    CallState,
    DaySummary,
    Direction,
    ExternalEvent,
    Fact,
    FaqChunk,
    FactSource,
    FactType,
    Channel,
    GameScore,
    InteractionModality,
    Person,
    TranscriptChunk,
)

try:
    import sqlite_vec  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - depends on optional install.
    sqlite_vec = None

# ── Schema ──────────────────────────────────────────────────────────────────

INITIAL_SCHEMA = """
-- Who we know
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY,
  phone TEXT UNIQUE NOT NULL,
  name TEXT,
  summary TEXT,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);

-- What happened
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  external_id TEXT UNIQUE,
  person_id TEXT NOT NULL REFERENCES people(id),
  direction TEXT NOT NULL,
  channel TEXT NOT NULL,
  modality TEXT NOT NULL,
  audience TEXT NOT NULL,
  action_id TEXT REFERENCES actions(id),
  transcript TEXT,
  summary TEXT,
  facts_extracted INTEGER NOT NULL DEFAULT 0,
  commitments_extracted INTEGER NOT NULL DEFAULT 0,
  extraction_retries INTEGER NOT NULL DEFAULT 0,
  extraction_error TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  duration INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_person ON calls(person_id, started_at);
CREATE INDEX IF NOT EXISTS idx_calls_extraction ON calls(facts_extracted, commitments_extracted)
  WHERE facts_extracted = 0 OR commitments_extracted = 0;

-- Active call state persisted across restarts
CREATE TABLE IF NOT EXISTS active_calls (
  call_id TEXT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
  person_id TEXT NOT NULL REFERENCES people(id),
  person_name TEXT,
  audience TEXT NOT NULL,
  direction TEXT NOT NULL,
  channel TEXT NOT NULL,
  modality TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_active_calls_started ON active_calls(started_at);
CREATE INDEX IF NOT EXISTS idx_active_calls_direction ON active_calls(direction, started_at);

-- Searchable transcript chunks
CREATE TABLE IF NOT EXISTS transcript_chunks (
  id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES calls(id),
  person_id TEXT NOT NULL REFERENCES people(id),
  content TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  embedding BLOB,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tc_call ON transcript_chunks(call_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_tc_person ON transcript_chunks(person_id);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_chunks_fts USING fts5(
  content, content=transcript_chunks, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS tc_fts_insert AFTER INSERT ON transcript_chunks BEGIN
  INSERT INTO transcript_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS tc_fts_delete AFTER DELETE ON transcript_chunks BEGIN
  INSERT INTO transcript_chunks_fts(transcript_chunks_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS tc_fts_update AFTER UPDATE ON transcript_chunks BEGIN
  INSERT INTO transcript_chunks_fts(transcript_chunks_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO transcript_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- Semantic facts
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES people(id),
  call_id TEXT REFERENCES calls(id),
  source_text TEXT,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL,
  embedding BLOB,
  verified_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  superseded_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_facts_person ON facts(person_id, superseded_at);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  content, content=facts, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_delete AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_update_live AFTER UPDATE ON facts
WHEN new.superseded_at IS NULL BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO facts_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_update_superseded AFTER UPDATE ON facts
WHEN new.superseded_at IS NOT NULL AND old.superseded_at IS NULL BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;

-- What needs to happen
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  person_id TEXT REFERENCES people(id),
  call_id TEXT REFERENCES calls(id),
  source_text TEXT,
  intent TEXT NOT NULL,
  context TEXT,
  due_at INTEGER,
  urgency TEXT NOT NULL DEFAULT 'normal',
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_attempted_at INTEGER,
  result TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, due_at);
CREATE INDEX IF NOT EXISTS idx_actions_person ON actions(person_id);

-- FAQ chunks
CREATE TABLE IF NOT EXISTS faq_chunks (
  id TEXT PRIMARY KEY,
  file_path TEXT NOT NULL,
  heading TEXT,
  content TEXT NOT NULL,
  embedding BLOB,
  updated_at INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(
  content, content=faq_chunks, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS faq_fts_insert AFTER INSERT ON faq_chunks BEGIN
  INSERT INTO faq_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS faq_fts_delete AFTER DELETE ON faq_chunks BEGIN
  INSERT INTO faq_fts(faq_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS faq_fts_update AFTER UPDATE ON faq_chunks BEGIN
  INSERT INTO faq_fts(faq_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
  INSERT INTO faq_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', 1);
"""

CALLS_ANSWERED_AT_MIGRATION = """
ALTER TABLE calls ADD COLUMN answered_at INTEGER;
"""

CALLS_LAST_EXTRACTION_ATTEMPT_AT_MIGRATION = """
ALTER TABLE calls ADD COLUMN last_extraction_attempt_at INTEGER;
"""

CALENDAR_PHASE_1_MIGRATION = """
ALTER TABLE actions ADD COLUMN start_at INTEGER;
ALTER TABLE actions ADD COLUMN end_at INTEGER;
CREATE TABLE IF NOT EXISTS external_events (
  id TEXT PRIMARY KEY,
  ics_uid TEXT NOT NULL,
  ics_url TEXT NOT NULL,
  title TEXT NOT NULL,
  start_at INTEGER NOT NULL,
  end_at INTEGER NOT NULL,
  all_day INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  location TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(ics_uid, ics_url)
);
CREATE INDEX IF NOT EXISTS idx_external_events_range ON external_events(start_at, end_at);
"""

CALENDAR_PHASE_2_MIGRATION = """
ALTER TABLE actions ADD COLUMN hub_event_id TEXT;
ALTER TABLE actions ADD COLUMN hub_sync_status TEXT;
ALTER TABLE actions ADD COLUMN hub_sync_attempts INTEGER NOT NULL DEFAULT 0;
"""

DAY_SUMMARIES_MIGRATION = """
CREATE TABLE IF NOT EXISTS day_summaries (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES people(id),
  date TEXT NOT NULL,
  summary TEXT,
  facts_extracted INTEGER NOT NULL DEFAULT 0,
  commitments_extracted INTEGER NOT NULL DEFAULT 0,
  extraction_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(person_id, date)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_day_summaries_person_date
  ON day_summaries(person_id, date);
"""

CHANNEL_MIGRATION = """
ALTER TABLE calls ADD COLUMN channel TEXT NOT NULL DEFAULT 'phone';
ALTER TABLE calls ADD COLUMN modality TEXT NOT NULL DEFAULT 'voice';
ALTER TABLE active_calls ADD COLUMN channel TEXT NOT NULL DEFAULT 'phone';
ALTER TABLE active_calls ADD COLUMN modality TEXT NOT NULL DEFAULT 'voice';
"""

GAME_MODE_AND_SCORES_MIGRATION = """
ALTER TABLE calls ADD COLUMN is_game_mode INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS game_scores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  score INTEGER NOT NULL,
  wave INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_scores_score ON game_scores(score DESC, created_at DESC);
"""

DROP_IS_GAME_MODE_MIGRATION = """
ALTER TABLE calls DROP COLUMN is_game_mode;
"""

BUILTIN_MIGRATIONS: Final[dict[int, str]] = {
    1: INITIAL_SCHEMA,
    2: CALLS_ANSWERED_AT_MIGRATION,
    3: CALLS_LAST_EXTRACTION_ATTEMPT_AT_MIGRATION,
    4: CALENDAR_PHASE_1_MIGRATION,
    5: CALENDAR_PHASE_2_MIGRATION,
    6: DAY_SUMMARIES_MIGRATION,
    7: CHANNEL_MIGRATION,
    8: GAME_MODE_AND_SCORES_MIGRATION,
    9: DROP_IS_GAME_MODE_MIGRATION,
}

def _vec_schema(dimensions: int) -> str:
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_chunks_vec USING vec0(
  chunk_rowid INTEGER PRIMARY KEY,
  embedding float[{dimensions}]
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
  fact_rowid INTEGER PRIMARY KEY,
  embedding float[{dimensions}]
);
CREATE VIRTUAL TABLE IF NOT EXISTS faq_vec USING vec0(
  chunk_rowid INTEGER PRIMARY KEY,
  embedding float[{dimensions}]
);
"""

VEC_FALLBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_chunks_vec (
  chunk_rowid INTEGER PRIMARY KEY,
  embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS facts_vec (
  fact_rowid INTEGER PRIMARY KEY,
  embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS faq_vec (
  chunk_rowid INTEGER PRIMARY KEY,
  embedding BLOB NOT NULL
);
"""
_VEC_DIMENSION_RE = re.compile(r"float\[(\d+)\]", re.IGNORECASE)

# ── Connection ───────────────────────────────────────────────────────────────

MEMORY_DB: Final[str] = ":memory:"


def get_db_path() -> Path:
    return get_home() / "mystic-horizon.db"


def open_database(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = _resolve_db_path(path)
    if db_path != MEMORY_DB:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")

    _load_sqlite_vec(db)
    logger.debug("db.opened", path=db_path)
    return db


def initialize_schema(db: sqlite3.Connection, dimensions: int | None = None) -> None:
    if dimensions is None:
        try:
            dimensions = get_embedding_dimensions()
        except Exception:
            dimensions = 256
    if dimensions <= 0:
        dimensions = 256
    db.executescript(INITIAL_SCHEMA)
    _initialize_vec_schema(db, dimensions)
    db.commit()
    version = run_migrations(db)
    logger.info("db.schema.initialized", version=version)



def close_database(db: sqlite3.Connection) -> None:
    db.close()
    logger.debug("db.closed")


def _resolve_db_path(path: str | Path | None) -> str:
    if path is None:
        return str(get_db_path())
    if isinstance(path, Path):
        return str(path.expanduser())
    if path == MEMORY_DB:
        return MEMORY_DB
    return str(Path(path).expanduser())


def _load_sqlite_vec(db: sqlite3.Connection) -> None:
    if sqlite_vec is None:
        logger.warn("db.sqlite_vec.unavailable")
        return

    try:
        load_fn = getattr(sqlite_vec, "load")
        load_fn(db)
        logger.debug("db.sqlite_vec.loaded")
    except Exception as exc:  # pragma: no cover - depends on local extension setup.
        logger.warn("db.sqlite_vec.load_failed", error=get_error_message(exc))


def _initialize_vec_schema(db: sqlite3.Connection, dimensions: int) -> None:
    _rebuild_vec_tables_if_needed(db, dimensions)
    try:
        db.executescript(_vec_schema(dimensions))
    except sqlite3.OperationalError as exc:
        if "vec0" not in str(exc):
            raise
        db.executescript(VEC_FALLBACK_SCHEMA)
        logger.warn("db.sqlite_vec.fallback_tables", error=get_error_message(exc))


def _detect_vec_dimension(db: sqlite3.Connection) -> int | None:
    for table_name in ("transcript_chunks_vec", "facts_vec", "faq_vec"):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if row is None:
            continue
        raw_sql = row["sql"]
        if raw_sql is None:
            continue
        sql = str(raw_sql)
        if "VIRTUAL TABLE" not in sql.upper() or "VEC0" not in sql.upper():
            return None
        match = _VEC_DIMENSION_RE.search(sql)
        if match is None:
            continue
        return int(match.group(1))
    return None


def _rebuild_vec_tables_if_needed(db: sqlite3.Connection, target_dim: int) -> None:
    current_dim = _detect_vec_dimension(db)
    if current_dim is None or current_dim == target_dim:
        return

    db.executescript(
        """
DROP TABLE IF EXISTS transcript_chunks_vec;
DROP TABLE IF EXISTS facts_vec;
DROP TABLE IF EXISTS faq_vec;
"""
    )
    db.execute("UPDATE transcript_chunks SET embedding = NULL")
    db.execute("UPDATE facts SET embedding = NULL")
    db.execute("UPDATE faq_chunks SET embedding = NULL")
    logger.info("db.vec.dimension.changed", old=current_dim, new=target_dim)


def _get_table_columns(db: sqlite3.Connection, table_name: str) -> set[str]:
    rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


# ── Migrations ───────────────────────────────────────────────────────────────

MigrationValidator = Callable[[sqlite3.Connection], bool]


def get_migrations_dir() -> Path:
    return get_home() / "migrations"


def get_schema_version(db: sqlite3.Connection) -> int:
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
    except sqlite3.Error:
        return 0

    if row is None:
        return 0

    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def get_applied_migrations(db: sqlite3.Connection) -> list[str]:
    write_builtin_migrations()
    current_version = get_schema_version(db)
    return [path.name for version, path in _iter_migration_files() if version <= current_version]


def run_migrations(db: sqlite3.Connection) -> int:
    write_builtin_migrations()

    current_version = get_schema_version(db)
    applied = 0

    for version, file_path in _iter_migration_files():
        if version <= current_version:
            continue

        validator = _MIGRATION_VALIDATORS.get(version)
        if validator is not None and validator(db):
            _set_schema_version(db, version)
            applied += 1
            current_version = version
            logger.info("db.migration.already_satisfied", version=version, file=file_path.name)
            continue

        _apply_migration(db, file_path, version)

        applied += 1
        current_version = version
        logger.info("db.migration.applied", version=version, file=file_path.name)

    if applied > 0:
        logger.info("db.migrations.complete", applied=applied, version=current_version)

    return current_version


def ensure_migrations_dir() -> Path:
    migrations_dir = get_migrations_dir()
    migrations_dir.mkdir(parents=True, exist_ok=True)
    return migrations_dir


def write_initial_migration(schema: str) -> Path:
    return _write_migration_file(1, schema)


def write_builtin_migrations() -> list[Path]:
    written: list[Path] = []
    for version, sql in BUILTIN_MIGRATIONS.items():
        written.append(_write_migration_file(version, sql))
    return written


def create_migration(name: str) -> Path:
    write_builtin_migrations()
    normalized_name = _slugify_migration_name(name)
    next_version = 1
    for version, _file_path in _iter_migration_files():
        if version >= next_version:
            next_version = version + 1
    file_path = ensure_migrations_dir() / _migration_file_name(next_version, normalized_name or None)
    if file_path.exists():
        raise FileExistsError(f"Migration already exists: {file_path.name}")

    title = normalized_name or "migration"
    file_path.write_text(
        (
            f"-- Migration {next_version:03d}: {title}\n"
            "-- Forward-only SQL. The runner wraps this file in a transaction.\n\n"
        ),
        encoding="utf-8",
    )
    logger.info("db.migration.created", version=next_version, file=file_path.name)
    return file_path


def _write_migration_file(version: int, sql: str, *, suffix: str | None = None) -> Path:
    file_path = ensure_migrations_dir() / _migration_file_name(version, suffix)
    if not file_path.exists():
        file_path.write_text(sql, encoding="utf-8")
    return file_path


def _migration_file_name(version: int, suffix: str | None = None) -> str:
    if suffix:
        return f"{version:03d}_{suffix}.sql"
    return f"{version:03d}.sql"


def _slugify_migration_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _iter_migration_files() -> list[tuple[int, Path]]:
    migrations_dir = get_migrations_dir()
    if not migrations_dir.exists():
        return []

    seen_versions: dict[int, Path] = {}
    migrations: list[tuple[int, Path]] = []
    for file_path in migrations_dir.glob("*.sql"):
        version = _parse_migration_version(file_path)
        if version is None:
            continue
        if version in seen_versions:
            other = seen_versions[version]
            raise ValueError(
                f"Duplicate migration version {version:03d}: {other.name} and {file_path.name}"
            )
        seen_versions[version] = file_path
        migrations.append((version, file_path))
    migrations.sort(key=lambda item: item[0])
    return migrations


def _parse_migration_version(file_path: Path) -> int | None:
    match = re.fullmatch(r"(?P<version>\d+)(?:[_-].+)?", file_path.stem)
    if match is None:
        return None
    return int(match.group("version"))


def _apply_migration(db: sqlite3.Connection, file_path: Path, version: int) -> None:
    sql = file_path.read_text(encoding="utf-8").strip()
    statements = [
        "BEGIN IMMEDIATE;",
        sql,
        _schema_version_upsert_sql(version),
        "COMMIT;",
    ]
    script = "\n".join(statement for statement in statements if statement) + "\n"
    try:
        db.executescript(script)
    except sqlite3.Error:
        db.rollback()
        raise


def _schema_version_upsert_sql(version: int) -> str:
    return (
        "INSERT INTO meta (key, value) VALUES ('schema_version', {version}) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
    ).format(version=version)


def _set_schema_version(db: sqlite3.Connection, version: int) -> None:
    with db:
        db.execute(
            """
            INSERT INTO meta (key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (version,),
        )


def _calls_has_answered_at(db: sqlite3.Connection) -> bool:
    return "answered_at" in _get_table_columns(db, "calls")


def _calls_has_last_extraction_attempt_at(db: sqlite3.Connection) -> bool:
    return "last_extraction_attempt_at" in _get_table_columns(db, "calls")


def _calendar_phase_1_ready(db: sqlite3.Connection) -> bool:
    action_columns = _get_table_columns(db, "actions")
    event_columns = _get_table_columns(db, "external_events")
    return {"start_at", "end_at"}.issubset(action_columns) and {
        "id",
        "ics_uid",
        "ics_url",
        "title",
        "start_at",
        "end_at",
        "all_day",
        "description",
        "location",
        "created_at",
        "updated_at",
    }.issubset(event_columns)


def _calendar_phase_2_ready(db: sqlite3.Connection) -> bool:
    action_columns = _get_table_columns(db, "actions")
    return {"hub_event_id", "hub_sync_status", "hub_sync_attempts"}.issubset(action_columns)


def _day_summaries_ready(db: sqlite3.Connection) -> bool:
    columns = _get_table_columns(db, "day_summaries")
    return {
        "id",
        "person_id",
        "date",
        "summary",
        "facts_extracted",
        "commitments_extracted",
        "extraction_error",
        "created_at",
        "updated_at",
    }.issubset(columns)


def _channel_ready(db: sqlite3.Connection) -> bool:
    call_columns = _get_table_columns(db, "calls")
    active_columns = _get_table_columns(db, "active_calls")
    return {"channel", "modality"}.issubset(call_columns) and (
        {"channel", "modality"}.issubset(active_columns)
    )


def _game_scores_ready(db: sqlite3.Connection) -> bool:
    score_columns = _get_table_columns(db, "game_scores")
    return {
        "id",
        "name",
        "score",
        "wave",
        "created_at",
    }.issubset(score_columns)


def _is_game_mode_dropped(db: sqlite3.Connection) -> bool:
    call_columns = _get_table_columns(db, "calls")
    return "is_game_mode" not in call_columns


_MIGRATION_VALIDATORS: Final[dict[int, MigrationValidator]] = {
    2: _calls_has_answered_at,
    3: _calls_has_last_extraction_attempt_at,
    4: _calendar_phase_1_ready,
    5: _calendar_phase_2_ready,
    6: _day_summaries_ready,
    7: _channel_ready,
    8: _game_scores_ready,
    9: _is_game_mode_dropped,
}


# ── Utils ────────────────────────────────────────────────────────────────────

T = TypeVar("T")
EmbeddingInput = bytes | bytearray | memoryview | Sequence[float]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return str(uuid.uuid4())


def parse_due_at(value: str | None) -> int | None:
    """Parse an ISO 8601 date string to milliseconds since epoch, or None."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def format_due_at(due_at: int | None) -> str:
    """Format a due-at timestamp (ms) as an ISO string, or 'ASAP' if None."""
    if due_at is None:
        return "ASAP"
    return datetime.fromtimestamp(due_at / 1000, tz=UTC).isoformat(timespec="minutes")


def _get_agent_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(get_agent_config().hours.timezone)
    except Exception:
        return ZoneInfo("UTC")


def _date_bounds_ms(date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(date)
    tz = _get_agent_timezone()
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    else:
        start = start.astimezone(tz)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _local_date_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, _get_agent_timezone()).strftime("%Y-%m-%d")


def row_to_dataclass(row: sqlite3.Row | None, cls: type[T]) -> T | None:
    if row is None:
        return None
    return cls(**dict(row))


def rows_to_dataclasses(rows: Iterable[sqlite3.Row], cls: type[T]) -> list[T]:
    return [cls(**dict(row)) for row in rows]


def pack_embedding(embedding: EmbeddingInput | None) -> bytes | None:
    if embedding is None:
        return None
    if isinstance(embedding, bytes):
        return embedding
    if isinstance(embedding, bytearray):
        return bytes(embedding)
    if isinstance(embedding, memoryview):
        return embedding.tobytes()
    return array("f", embedding).tobytes()


def get_rowid(
    db: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    id_value: str,
) -> int | None:
    row = db.execute(
        f"SELECT rowid FROM {table} WHERE {id_column} = ?",
        (id_value,),
    ).fetchone()
    if row is None:
        return None
    return int(row["rowid"])


def upsert_vec_row(
    db: sqlite3.Connection,
    *,
    table: str,
    rowid_column: str,
    rowid: int,
    embedding: bytes,
) -> None:
    # Table and column names are fixed internal constants.
    db.execute(f"DELETE FROM {table} WHERE {rowid_column} = ?", (rowid,))
    db.execute(
        f"INSERT INTO {table} ({rowid_column}, embedding) VALUES (CAST(? AS INTEGER), ?)",
        (rowid, embedding),
    )


def delete_vec_rows(
    db: sqlite3.Connection,
    *,
    table: str,
    rowid_column: str,
    rowids: Iterable[int],
) -> None:
    values = list(rowids)
    if not values:
        return
    for rowid in values:
        db.execute(f"DELETE FROM {table} WHERE {rowid_column} = ?", (rowid,))


# ── people ───────────────────────────────────────────────────────────────────

def upsert_person(db: sqlite3.Connection, phone: str, name: str | None = None) -> Person:
    now = now_ms()
    existing = get_person_by_phone(db, phone)
    if existing is not None:
        if name and not existing.name:
            with db:
                db.execute(
                    "UPDATE people SET name = ?, last_seen = ? WHERE id = ?",
                    (name, now, existing.id),
                )
            return Person(
                id=existing.id,
                phone=existing.phone,
                name=name,
                summary=existing.summary,
                first_seen=existing.first_seen,
                last_seen=now,
            )

        with db:
            db.execute("UPDATE people SET last_seen = ? WHERE id = ?", (now, existing.id))
        existing.last_seen = now
        return existing

    person = Person(
        id=new_id(),
        phone=phone,
        name=name,
        summary=None,
        first_seen=now,
        last_seen=now,
    )
    with db:
        db.execute(
            "INSERT INTO people (id, phone, name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (person.id, person.phone, person.name, person.first_seen, person.last_seen),
        )
    return person


def get_person_by_id(db: sqlite3.Connection, person_id: str) -> Person | None:
    row = db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    return row_to_dataclass(row, Person)


def get_person_by_phone(db: sqlite3.Connection, phone: str) -> Person | None:
    row = db.execute("SELECT * FROM people WHERE phone = ?", (phone,)).fetchone()
    return row_to_dataclass(row, Person)


def find_people(db: sqlite3.Connection, query: str) -> list[Person]:
    rows = db.execute(
        "SELECT * FROM people WHERE name LIKE ? OR phone LIKE ? ORDER BY last_seen DESC LIMIT 20",
        (f"%{query}%", f"%{query}%"),
    ).fetchall()
    return rows_to_dataclasses(rows, Person)


def get_all_people(db: sqlite3.Connection, limit: int = 100) -> list[Person]:
    rows = db.execute(
        "SELECT * FROM people ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, Person)


def update_person_name(db: sqlite3.Connection, phone: str, name: str) -> Person | None:
    with db:
        db.execute("UPDATE people SET name = ? WHERE phone = ?", (name, phone))
    return get_person_by_phone(db, phone)


def update_person_summary(db: sqlite3.Connection, person_id: str, summary: str) -> None:
    with db:
        db.execute("UPDATE people SET summary = ? WHERE id = ?", (summary, person_id))


def update_person_last_seen(db: sqlite3.Connection, person_id: str) -> None:
    with db:
        db.execute("UPDATE people SET last_seen = ? WHERE id = ?", (now_ms(), person_id))


# ── calls ────────────────────────────────────────────────────────────────────

def insert_call(
    db: sqlite3.Connection,
    *,
    person_id: str,
    direction: Direction,
    audience: Audience,
    channel: Channel = "phone",
    modality: InteractionModality = "voice",
    call_id: str | None = None,
    external_id: str | None = None,
    action_id: str | None = None,
) -> Call:
    created = Call(
        id=call_id or new_id(),
        external_id=external_id,
        person_id=person_id,
        direction=direction,
        channel=channel,
        modality=modality,
        audience=audience,
        action_id=action_id,
        transcript=None,
        summary=None,
        facts_extracted=0,
        commitments_extracted=0,
        extraction_retries=0,
        extraction_error=None,
        last_extraction_attempt_at=None,
        started_at=now_ms(),
        answered_at=None,
        ended_at=None,
        duration=None,
    )
    with db:
        db.execute(
            """
            INSERT INTO calls (
              id, external_id, person_id, direction, channel, modality, audience, action_id, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created.id,
                created.external_id,
                created.person_id,
                created.direction,
                created.channel,
                created.modality,
                created.audience,
                created.action_id,
                created.started_at,
            ),
        )
    stored = get_call_by_id(db, created.id)
    if stored is None:  # pragma: no cover - defensive.
        raise RuntimeError(f"Call insert failed for {created.id}")
    return stored


def get_call_by_id(db: sqlite3.Connection, call_id: str) -> Call | None:
    row = db.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return row_to_dataclass(row, Call)


def get_call_transcript(db: sqlite3.Connection, call_id: str) -> str | None:
    row = db.execute("SELECT transcript FROM calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        return None
    transcript = row["transcript"]
    return str(transcript) if transcript is not None else None


def get_call_by_external_id(db: sqlite3.Connection, external_id: str) -> Call | None:
    row = db.execute("SELECT * FROM calls WHERE external_id = ?", (external_id,)).fetchone()
    return row_to_dataclass(row, Call)


def update_call_end(
    db: sqlite3.Connection,
    call_id: str,
    *,
    transcript: str | None = None,
    ended_at: int | None = None,
    duration: int | None = None,
) -> None:
    with db:
        db.execute(
            """
            UPDATE calls SET
              transcript = COALESCE(?, transcript),
              ended_at = COALESCE(?, ?),
              duration = COALESCE(?, duration)
            WHERE id = ?
            """,
            (transcript, ended_at, now_ms(), duration, call_id),
        )


def update_call_external_id(db: sqlite3.Connection, call_id: str, external_id: str) -> None:
    with db:
        db.execute("UPDATE calls SET external_id = ? WHERE id = ?", (external_id, call_id))


def delete_call_by_id(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute("DELETE FROM calls WHERE id = ?", (call_id,))


def update_call_summary(db: sqlite3.Connection, call_id: str, summary: str) -> None:
    with db:
        db.execute("UPDATE calls SET summary = ? WHERE id = ?", (summary, call_id))


def update_call_transcript(db: sqlite3.Connection, call_id: str, transcript: str) -> None:
    with db:
        db.execute("UPDATE calls SET transcript = ? WHERE id = ?", (transcript, call_id))


def append_call_transcript(db: sqlite3.Connection, call_id: str, transcript_delta: str) -> None:
    with db:
        db.execute(
            """
            UPDATE calls SET transcript = CASE
              WHEN transcript IS NULL OR transcript = '' THEN ?
              ELSE transcript || '\n' || ?
            END
            WHERE id = ?
            """,
            (transcript_delta, transcript_delta, call_id),
        )


def update_call_answered_at(
    db: sqlite3.Connection,
    call_id: str,
    answered_at: int | None = None,
) -> None:
    with db:
        db.execute(
            "UPDATE calls SET answered_at = COALESCE(answered_at, ?) WHERE id = ?",
            (answered_at or now_ms(), call_id),
        )


def insert_game_score(
    db: sqlite3.Connection, *, name: str, score: int, wave: int
) -> GameScore:
    row = GameScore(
        id=new_id(),
        name=name,
        score=score,
        wave=wave,
        created_at=now_ms(),
    )
    with db:
        db.execute(
            "INSERT INTO game_scores (id, name, score, wave, created_at) VALUES (?, ?, ?, ?, ?)",
            (row.id, row.name, row.score, row.wave, row.created_at),
        )
    return row


def top_game_scores(db: sqlite3.Connection, *, limit: int = 10) -> list[GameScore]:
    rows = db.execute(
        """
        SELECT id, name, score, wave, created_at
        FROM game_scores
        ORDER BY score DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, GameScore)


def rank_for_score(db: sqlite3.Connection, score: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS better FROM game_scores WHERE score > ?",
        (score,),
    ).fetchone()
    better = int(row["better"]) if row is not None else 0
    return better + 1


def previous_best_game_score(db: sqlite3.Connection) -> int | None:
    row = db.execute(
        "SELECT MAX(score) AS best FROM game_scores"
    ).fetchone()
    if row is None or row["best"] is None:
        return None
    return int(row["best"])


def mark_facts_extracted(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute("UPDATE calls SET facts_extracted = 1 WHERE id = ?", (call_id,))


def mark_commitments_extracted(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute("UPDATE calls SET commitments_extracted = 1 WHERE id = ?", (call_id,))


def mark_extraction_error(db: sqlite3.Connection, call_id: str, error: str) -> None:
    with db:
        db.execute(
            """
            UPDATE calls SET
              extraction_error = ?,
              extraction_retries = extraction_retries + 1,
              last_extraction_attempt_at = ?
            WHERE id = ?
            """,
            (error, now_ms(), call_id),
        )


def mark_extraction_attempted(
    db: sqlite3.Connection,
    call_id: str,
    attempted_at: int | None = None,
) -> None:
    with db:
        db.execute(
            "UPDATE calls SET last_extraction_attempt_at = ? WHERE id = ?",
            (attempted_at or now_ms(), call_id),
        )


def clear_extraction_error(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute(
            "UPDATE calls SET extraction_error = NULL, last_extraction_attempt_at = NULL WHERE id = ?",
            (call_id,),
        )


def get_calls_needing_extraction(db: sqlite3.Connection) -> list[Call]:
    rows = db.execute(
        """
        SELECT * FROM calls
        WHERE (
          facts_extracted = 0
          OR commitments_extracted = 0
          OR summary IS NULL
          OR NOT EXISTS (
            SELECT 1
            FROM transcript_chunks
            WHERE transcript_chunks.call_id = calls.id
          )
        )
          AND transcript IS NOT NULL
          AND extraction_retries < 5
        ORDER BY started_at ASC
        LIMIT 200
        """
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


def get_recent_calls_by_person(
    db: sqlite3.Connection,
    person_id: str,
    limit: int = 5,
) -> list[Call]:
    rows = db.execute(
        """
        SELECT * FROM calls
        WHERE person_id = ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (person_id, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


def get_recent_summarized_calls_by_person(
    db: sqlite3.Connection,
    person_id: str,
    limit: int = 20,
) -> list[Call]:
    rows = db.execute(
        """
        SELECT * FROM calls
        WHERE person_id = ?
          AND summary IS NOT NULL
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (person_id, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


def get_todays_calls(db: sqlite3.Connection) -> list[Call]:
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        """
        SELECT * FROM calls
        WHERE started_at >= ?
        ORDER BY started_at DESC
        LIMIT 200
        """,
        (int(today_start.timestamp() * 1000),),
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


def get_recent_calls(db: sqlite3.Connection, limit: int = 100) -> list[Call]:
    rows = db.execute(
        """
        SELECT * FROM calls
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


# ── day_summaries ────────────────────────────────────────────────────────────

def upsert_day_summary(
    db: sqlite3.Connection,
    person_id: str,
    date: str,
    summary: str | None = None,
) -> DaySummary:
    existing = get_day_summary(db, person_id, date)
    now = now_ms()

    if existing is None:
        created = DaySummary(
            id=new_id(),
            person_id=person_id,
            date=date,
            summary=summary,
            facts_extracted=0,
            commitments_extracted=0,
            extraction_error=None,
            created_at=now,
            updated_at=now,
        )
        with db:
            db.execute(
                """
                INSERT INTO day_summaries (
                  id, person_id, date, summary, facts_extracted, commitments_extracted,
                  extraction_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created.id,
                    created.person_id,
                    created.date,
                    created.summary,
                    created.facts_extracted,
                    created.commitments_extracted,
                    created.extraction_error,
                    created.created_at,
                    created.updated_at,
                ),
            )
        stored = get_day_summary(db, person_id, date)
        if stored is None:  # pragma: no cover - defensive.
            raise RuntimeError(f"Day summary insert failed for {person_id} {date}")
        return stored

    if summary is not None:
        with db:
            db.execute(
                "UPDATE day_summaries SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, now, existing.id),
            )
        refreshed = get_day_summary(db, person_id, date)
        if refreshed is not None:
            return refreshed
    return existing


def get_day_summary(db: sqlite3.Connection, person_id: str, date: str) -> DaySummary | None:
    row = db.execute(
        "SELECT * FROM day_summaries WHERE person_id = ? AND date = ?",
        (person_id, date),
    ).fetchone()
    return row_to_dataclass(row, DaySummary)


def update_day_summary(db: sqlite3.Connection, day_summary_id: str, summary: str) -> None:
    with db:
        db.execute(
            "UPDATE day_summaries SET summary = ?, updated_at = ? WHERE id = ?",
            (summary, now_ms(), day_summary_id),
        )


def mark_day_facts_extracted(db: sqlite3.Connection, day_summary_id: str) -> None:
    with db:
        db.execute(
            "UPDATE day_summaries SET facts_extracted = 1, updated_at = ? WHERE id = ?",
            (now_ms(), day_summary_id),
        )


def mark_day_commitments_extracted(db: sqlite3.Connection, day_summary_id: str) -> None:
    with db:
        db.execute(
            "UPDATE day_summaries SET commitments_extracted = 1, updated_at = ? WHERE id = ?",
            (now_ms(), day_summary_id),
        )


def mark_day_extraction_error(db: sqlite3.Connection, day_summary_id: str, error: str) -> None:
    with db:
        db.execute(
            "UPDATE day_summaries SET extraction_error = ?, updated_at = ? WHERE id = ?",
            (error, now_ms(), day_summary_id),
        )


def clear_day_extraction_error(db: sqlite3.Connection, day_summary_id: str) -> None:
    with db:
        db.execute(
            "UPDATE day_summaries SET extraction_error = NULL, updated_at = ? WHERE id = ?",
            (now_ms(), day_summary_id),
        )


def mark_day_extraction_complete(db: sqlite3.Connection, day_summary_id: str) -> None:
    with db:
        db.execute(
            """
            UPDATE day_summaries
            SET facts_extracted = 1,
                commitments_extracted = 1,
                extraction_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now_ms(), day_summary_id),
        )


def get_recent_day_summaries(
    db: sqlite3.Connection,
    person_id: str,
    limit: int = 7,
) -> list[DaySummary]:
    rows = db.execute(
        """
        SELECT * FROM day_summaries
        WHERE person_id = ?
          AND summary IS NOT NULL
        ORDER BY date DESC
        LIMIT ?
        """,
        (person_id, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, DaySummary)


def is_day_summary_finalized(summary: DaySummary | None) -> bool:
    if summary is None:
        return False
    return (
        bool((summary.summary or "").strip())
        and summary.facts_extracted == 1
        and summary.commitments_extracted == 1
        and summary.extraction_error is None
    )


def get_unfinalized_interactions(
    db: sqlite3.Connection,
    person_id: str,
    current_date: str,
) -> list[Call]:
    """Return verbatim interactions that still need prompt-level continuity."""

    _, end_ms = _date_bounds_ms(current_date)
    call_rows = db.execute(
        """
        SELECT * FROM calls
        WHERE person_id = ?
          AND started_at < ?
          AND transcript IS NOT NULL
          AND TRIM(transcript) != ''
        ORDER BY started_at ASC
        """,
        (person_id, end_ms),
    ).fetchall()
    calls = rows_to_dataclasses(call_rows, Call)
    if not calls:
        return []

    summary_rows = db.execute(
        """
        SELECT * FROM day_summaries
        WHERE person_id = ?
        """,
        (person_id,),
    ).fetchall()
    summaries = {
        summary.date: summary
        for summary in rows_to_dataclasses(summary_rows, DaySummary)
    }

    pending: list[Call] = []
    for call in calls:
        date = _local_date_key(call.started_at)
        if date == current_date or not is_day_summary_finalized(summaries.get(date)):
            pending.append(call)
    return pending


def get_today_interactions(
    db: sqlite3.Connection,
    person_id: str,
    date: str,
) -> list[Call]:
    start_ms, end_ms = _date_bounds_ms(date)
    rows = db.execute(
        """
        SELECT * FROM calls
        WHERE person_id = ?
          AND started_at >= ?
          AND started_at < ?
        ORDER BY started_at ASC
        """,
        (person_id, start_ms, end_ms),
    ).fetchall()
    return rows_to_dataclasses(rows, Call)


def get_people_with_interactions_on_date(
    db: sqlite3.Connection,
    date: str,
) -> list[str]:
    start_ms, end_ms = _date_bounds_ms(date)
    rows = db.execute(
        """
        SELECT person_id
        FROM calls
        WHERE started_at >= ?
          AND started_at < ?
          AND transcript IS NOT NULL
        GROUP BY person_id
        ORDER BY MIN(started_at) ASC
        """,
        (start_ms, end_ms),
    ).fetchall()
    return [str(row["person_id"]) for row in rows]


def get_days_needing_extraction(
    db: sqlite3.Connection,
    date: str,
) -> list[DaySummary]:
    pending: list[DaySummary] = []
    for person_id in get_people_with_interactions_on_date(db, date):
        summary = get_day_summary(db, person_id, date)
        if summary is None:
            summary = upsert_day_summary(db, person_id, date)
        if (
            summary.summary is None
            or summary.facts_extracted == 0
            or summary.commitments_extracted == 0
        ):
            pending.append(summary)
    return pending


# ── transcript_chunks ────────────────────────────────────────────────────────

def insert_transcript_chunk(
    db: sqlite3.Connection,
    *,
    call_id: str,
    person_id: str,
    content: str,
    chunk_index: int,
    embedding: Sequence[float] | bytes | bytearray | memoryview | None = None,
) -> TranscriptChunk:
    chunk = TranscriptChunk(
        id=new_id(),
        call_id=call_id,
        person_id=person_id,
        content=content,
        chunk_index=chunk_index,
        embedding=pack_embedding(embedding),
        created_at=now_ms(),
    )
    with db:
        _insert_transcript_chunk(db, chunk)
    return chunk


def delete_transcript_chunks_by_call_id(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        _delete_transcript_chunks_by_call_id(db, call_id)


def replace_transcript_chunks_for_call(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
    chunks: Sequence[dict[str, object]],
) -> int:
    prepared = [
        TranscriptChunk(
            id=new_id(),
            call_id=call_id,
            person_id=person_id,
            content=str(chunk["content"]),
            chunk_index=index,
            embedding=pack_embedding(chunk.get("embedding")),  # type: ignore[arg-type]
            created_at=now_ms(),
        )
        for index, chunk in enumerate(chunks)
    ]

    with db:
        _delete_transcript_chunks_by_call_id(db, call_id)
        for chunk in prepared:
            _insert_transcript_chunk(db, chunk)

    return len(prepared)


def get_chunks_by_call_id(db: sqlite3.Connection, call_id: str) -> list[TranscriptChunk]:
    rows = db.execute(
        "SELECT * FROM transcript_chunks WHERE call_id = ? ORDER BY chunk_index ASC",
        (call_id,),
    ).fetchall()
    return rows_to_dataclasses(rows, TranscriptChunk)


def get_chunks_with_null_embeddings(db: sqlite3.Connection) -> list[TranscriptChunk]:
    rows = db.execute("SELECT * FROM transcript_chunks WHERE embedding IS NULL").fetchall()
    return rows_to_dataclasses(rows, TranscriptChunk)


def update_chunk_embedding(
    db: sqlite3.Connection,
    chunk_id: str,
    embedding: Sequence[float] | bytes | bytearray | memoryview,
) -> None:
    embedding_blob = pack_embedding(embedding)
    if embedding_blob is None:  # pragma: no cover - defensive.
        return

    with db:
        db.execute(
            "UPDATE transcript_chunks SET embedding = ? WHERE id = ?",
            (embedding_blob, chunk_id),
        )
        rowid = get_rowid(
            db,
            table="transcript_chunks",
            id_column="id",
            id_value=chunk_id,
        )
        if rowid is not None:
            upsert_vec_row(
                db,
                table="transcript_chunks_vec",
                rowid_column="chunk_rowid",
                rowid=rowid,
                embedding=embedding_blob,
            )


def _insert_transcript_chunk(db: sqlite3.Connection, chunk: TranscriptChunk) -> None:
    db.execute(
        """
        INSERT INTO transcript_chunks (id, call_id, person_id, content, chunk_index, embedding, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.id,
            chunk.call_id,
            chunk.person_id,
            chunk.content,
            chunk.chunk_index,
            chunk.embedding,
            chunk.created_at,
        ),
    )
    if chunk.embedding is None:
        return

    rowid = get_rowid(db, table="transcript_chunks", id_column="id", id_value=chunk.id)
    if rowid is None:
        return
    upsert_vec_row(
        db,
        table="transcript_chunks_vec",
        rowid_column="chunk_rowid",
        rowid=rowid,
        embedding=chunk.embedding,
    )


def _delete_transcript_chunks_by_call_id(db: sqlite3.Connection, call_id: str) -> None:
    rowids = [
        int(row["rowid"])
        for row in db.execute(
            "SELECT rowid FROM transcript_chunks WHERE call_id = ?",
            (call_id,),
        ).fetchall()
    ]
    delete_vec_rows(
        db,
        table="transcript_chunks_vec",
        rowid_column="chunk_rowid",
        rowids=rowids,
    )
    db.execute("DELETE FROM transcript_chunks WHERE call_id = ?", (call_id,))


# ── facts ────────────────────────────────────────────────────────────────────

def insert_fact(
    db: sqlite3.Connection,
    *,
    person_id: str,
    type: FactType,
    content: str,
    confidence: float,
    source: FactSource,
    call_id: str | None = None,
    source_text: str | None = None,
    embedding: Sequence[float] | bytes | bytearray | memoryview | None = None,
) -> Fact:
    now = now_ms()
    fact = Fact(
        id=new_id(),
        person_id=person_id,
        call_id=call_id,
        source_text=source_text,
        type=type,
        content=content,
        confidence=confidence,
        source=source,
        embedding=pack_embedding(embedding),
        verified_at=now,
        created_at=now,
        superseded_at=None,
    )
    with db:
        db.execute(
            """
            INSERT INTO facts (
              id, person_id, call_id, source_text, type, content, confidence, source, embedding,
              verified_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.id,
                fact.person_id,
                fact.call_id,
                fact.source_text,
                fact.type,
                fact.content,
                fact.confidence,
                fact.source,
                fact.embedding,
                fact.verified_at,
                fact.created_at,
            ),
        )
        if fact.embedding is not None:
            rowid = get_rowid(db, table="facts", id_column="id", id_value=fact.id)
            if rowid is not None:
                upsert_vec_row(
                    db,
                    table="facts_vec",
                    rowid_column="fact_rowid",
                    rowid=rowid,
                    embedding=fact.embedding,
                )
    return fact


def delete_post_call_facts_by_call_id(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        rowids = [
            int(row["rowid"])
            for row in db.execute(
                "SELECT rowid FROM facts WHERE call_id = ? AND source = 'post-call'",
                (call_id,),
            ).fetchall()
        ]
        delete_vec_rows(db, table="facts_vec", rowid_column="fact_rowid", rowids=rowids)
        db.execute(
            "DELETE FROM facts WHERE call_id = ? AND source = 'post-call'",
            (call_id,),
        )


def get_active_facts_by_person(
    db: sqlite3.Connection,
    person_id: str,
    limit: int = 10,
) -> list[Fact]:
    rows = db.execute(
        """
        SELECT * FROM facts
        WHERE person_id = ? AND superseded_at IS NULL
        ORDER BY confidence DESC
        LIMIT ?
        """,
        (person_id, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Fact)


def get_all_active_facts_by_person(
    db: sqlite3.Connection,
    person_id: str,
    limit: int = 500,
) -> list[Fact]:
    return get_active_facts_by_person(db, person_id, limit)


def search_facts(
    db: sqlite3.Connection,
    query: str,
    *,
    person_id: str | None = None,
    limit: int = 50,
) -> list[Fact]:
    if person_id:
        rows = db.execute(
            """
            SELECT * FROM facts
            WHERE superseded_at IS NULL
              AND person_id = ?
              AND content LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (person_id, f"%{query}%", limit),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT * FROM facts
            WHERE superseded_at IS NULL
              AND content LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (f"%{query}%", limit),
        ).fetchall()
    return rows_to_dataclasses(rows, Fact)


def get_fact_by_id(db: sqlite3.Connection, fact_id: str) -> Fact | None:
    row = db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return row_to_dataclass(row, Fact)


def supersede_fact(db: sqlite3.Connection, fact_id: str) -> None:
    with db:
        rowid = get_rowid(db, table="facts", id_column="id", id_value=fact_id)
        db.execute("UPDATE facts SET superseded_at = ? WHERE id = ?", (now_ms(), fact_id))
        if rowid is not None:
            delete_vec_rows(db, table="facts_vec", rowid_column="fact_rowid", rowids=[rowid])


def bump_fact_confidence(db: sqlite3.Connection, fact_id: str, new_confidence: float) -> None:
    now = now_ms()
    with db:
        db.execute(
            "UPDATE facts SET confidence = MAX(confidence, ?), verified_at = ? WHERE id = ?",
            (new_confidence, now, fact_id),
        )


def update_fact_embedding(
    db: sqlite3.Connection,
    fact_id: str,
    embedding: Sequence[float] | bytes | bytearray | memoryview,
) -> None:
    embedding_blob = pack_embedding(embedding)
    if embedding_blob is None:  # pragma: no cover - defensive.
        return

    with db:
        db.execute("UPDATE facts SET embedding = ? WHERE id = ?", (embedding_blob, fact_id))
        rowid = get_rowid(db, table="facts", id_column="id", id_value=fact_id)
        if rowid is not None:
            upsert_vec_row(
                db,
                table="facts_vec",
                rowid_column="fact_rowid",
                rowid=rowid,
                embedding=embedding_blob,
            )


def get_facts_with_null_embeddings(db: sqlite3.Connection) -> list[Fact]:
    rows = db.execute(
        "SELECT * FROM facts WHERE embedding IS NULL AND superseded_at IS NULL LIMIT 100",
    ).fetchall()
    return rows_to_dataclasses(rows, Fact)


# ── actions ──────────────────────────────────────────────────────────────────

def insert_action(
    db: sqlite3.Connection,
    *,
    intent: str,
    source: ActionSource,
    person_id: str | None = None,
    call_id: str | None = None,
    source_text: str | None = None,
    context: str | None = None,
    due_at: int | None = None,
    urgency: ActionUrgency = "normal",
    start_at: int | None = None,
    end_at: int | None = None,
    hub_sync_status: str | None = None,
) -> Action:
    now = now_ms()
    action_id = new_id()
    effective_due_at = start_at if due_at is None and start_at is not None else due_at
    with db:
        db.execute(
            """
            INSERT INTO actions (
              id, person_id, call_id, source_text, intent, context, due_at, urgency, source,
              status, created_at, updated_at, start_at, end_at, hub_sync_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                person_id,
                call_id,
                source_text,
                intent,
                context,
                effective_due_at,
                urgency,
                source,
                now,
                now,
                start_at,
                end_at,
                hub_sync_status,
            ),
        )
    stored = get_action_by_id(db, action_id)
    if stored is None:  # pragma: no cover - defensive.
        raise RuntimeError(f"Action insert failed for {action_id}")
    return stored


def get_action_by_id(db: sqlite3.Connection, action_id: str) -> Action | None:
    row = db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return row_to_dataclass(row, Action)


def delete_post_call_actions_by_call_id(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute(
            "DELETE FROM actions WHERE call_id = ? AND source = 'post-call'",
            (call_id,),
        )


def get_due_actions(db: sqlite3.Connection) -> list[Action]:
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE status = 'pending'
          AND attempts < max_attempts
          AND (
            (due_at IS NULL AND start_at IS NULL)
            OR COALESCE(due_at, start_at) <= ?
          )
        ORDER BY
          CASE urgency WHEN 'high' THEN 1 ELSE 0 END DESC,
          CASE WHEN COALESCE(due_at, start_at) IS NULL THEN 0 ELSE 1 END ASC,
          COALESCE(due_at, start_at) ASC
        LIMIT 50
        """,
        (now_ms(),),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_pending_actions_by_person(db: sqlite3.Connection, person_id: str) -> list[Action]:
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE person_id = ? AND status = 'pending'
        ORDER BY due_at ASC
        LIMIT 50
        """,
        (person_id,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_open_actions_by_person(db: sqlite3.Connection, person_id: str) -> list[Action]:
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE person_id = ?
          AND status IN ('pending', 'in_progress')
        ORDER BY due_at ASC
        LIMIT 50
        """,
        (person_id,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_all_pending_actions(db: sqlite3.Connection) -> list[Action]:
    rows = db.execute(
        "SELECT * FROM actions WHERE status = 'pending' ORDER BY due_at ASC LIMIT 100",
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_failed_actions(db: sqlite3.Connection, since_days: int = 7) -> list[Action]:
    since = now_ms() - (since_days * 24 * 60 * 60 * 1000)
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE status = 'failed' AND updated_at >= ?
        ORDER BY updated_at DESC
        """,
        (since,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def update_action_status(
    db: sqlite3.Connection,
    action_id: str,
    status: ActionStatus,
    result: str | None = None,
    hub_sync_status: str | None = None,
) -> None:
    with db:
        db.execute(
            """
            UPDATE actions
            SET status = ?, result = COALESCE(?, result), hub_sync_status = COALESCE(?, hub_sync_status), updated_at = ?
            WHERE id = ?
            """,
            (status, result, hub_sync_status, now_ms(), action_id),
        )


def update_action_due_at(db: sqlite3.Connection, action_id: str, due_at: int | None) -> None:
    with db:
        db.execute(
            "UPDATE actions SET due_at = ?, updated_at = ? WHERE id = ?",
            (due_at, now_ms(), action_id),
        )


def update_action_time_slot(
    db: sqlite3.Connection,
    action_id: str,
    start_at: int | None,
    end_at: int | None,
    hub_sync_status: str | None = None,
) -> None:
    with db:
        db.execute(
            """
            UPDATE actions
            SET start_at = ?, end_at = ?, hub_sync_status = COALESCE(?, hub_sync_status), updated_at = ?
            WHERE id = ?
            """,
            (start_at, end_at, hub_sync_status, now_ms(), action_id),
        )


def get_actions_pending_hub_sync(
    db: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[Action]:
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE hub_sync_status = 'pending'
          AND start_at IS NOT NULL
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def mark_action_hub_synced(
    db: sqlite3.Connection,
    action_id: str,
    hub_event_id: str,
) -> None:
    with db:
        db.execute(
            """
            UPDATE actions
            SET hub_event_id = ?, hub_sync_status = 'synced', hub_sync_attempts = 0, updated_at = ?
            WHERE id = ?
            """,
            (hub_event_id, now_ms(), action_id),
        )


def mark_action_hub_pending(db: sqlite3.Connection, action_id: str) -> None:
    with db:
        db.execute(
            """
            UPDATE actions
            SET hub_sync_status = 'pending', hub_sync_attempts = 0, updated_at = ?
            WHERE id = ?
            """,
            (now_ms(), action_id),
        )


def increment_hub_sync_attempts(db: sqlite3.Connection, action_id: str) -> int:
    with db:
        db.execute(
            """
            UPDATE actions
            SET hub_sync_attempts = hub_sync_attempts + 1, updated_at = ?
            WHERE id = ?
            """,
            (now_ms(), action_id),
        )
    row = db.execute(
        "SELECT hub_sync_attempts FROM actions WHERE id = ?",
        (action_id,),
    ).fetchone()
    return int(row["hub_sync_attempts"]) if row is not None else 0


def mark_action_hub_failed(db: sqlite3.Connection, action_id: str) -> None:
    with db:
        db.execute(
            "UPDATE actions SET hub_sync_status = 'failed', updated_at = ? WHERE id = ?",
            (now_ms(), action_id),
        )


def clear_action_hub_event(db: sqlite3.Connection, action_id: str) -> None:
    with db:
        db.execute(
            """
            UPDATE actions
            SET hub_event_id = NULL, hub_sync_status = NULL, hub_sync_attempts = 0, updated_at = ?
            WHERE id = ?
            """,
            (now_ms(), action_id),
        )


def get_scheduled_actions_in_range(
    db: sqlite3.Connection,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 100,
) -> list[Action]:
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE start_at IS NOT NULL
          AND start_at < ?
          AND end_at > ?
          AND status IN ('pending', 'in_progress')
        ORDER BY start_at ASC
        LIMIT ?
        """,
        (end_ms, start_ms, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_upcoming_scheduled_actions(
    db: sqlite3.Connection,
    *,
    within_ms: int,
    limit: int = 50,
) -> list[Action]:
    now = now_ms()
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE start_at IS NOT NULL
          AND start_at >= ?
          AND start_at < ?
          AND status IN ('pending', 'in_progress')
        ORDER BY start_at ASC
        LIMIT ?
        """,
        (now, now + within_ms, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def get_in_progress_scheduled_actions(
    db: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[Action]:
    now = now_ms()
    rows = db.execute(
        """
        SELECT * FROM actions
        WHERE start_at IS NOT NULL
          AND start_at <= ?
          AND end_at > ?
          AND status IN ('pending', 'in_progress')
        ORDER BY start_at ASC
        LIMIT ?
        """,
        (now, now, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def increment_action_attempts(db: sqlite3.Connection, action_id: str) -> None:
    now = now_ms()
    with db:
        db.execute(
            """
            UPDATE actions
            SET attempts = attempts + 1, last_attempted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, action_id),
        )


def start_action_attempt(db: sqlite3.Connection, action_id: str) -> None:
    now = now_ms()
    with db:
        db.execute(
            """
            UPDATE actions SET
              status = 'in_progress',
              attempts = attempts + 1,
              last_attempted_at = ?,
              updated_at = ?
            WHERE id = ?
            """,
            (now, now, action_id),
        )


def reset_action_to_pending(
    db: sqlite3.Connection,
    action_id: str,
    due_at: int | None,
    result: str | None = None,
) -> None:
    with db:
        db.execute(
            """
            UPDATE actions SET
              status = 'pending',
              due_at = ?,
              result = COALESCE(?, result),
              updated_at = ?
            WHERE id = ?
            """,
            (due_at, result, now_ms(), action_id),
        )


def update_action_context(db: sqlite3.Connection, action_id: str, context: str) -> None:
    with db:
        db.execute(
            "UPDATE actions SET context = ?, updated_at = ? WHERE id = ?",
            (context, now_ms(), action_id),
        )


def get_actions_by_call_id(db: sqlite3.Connection, call_id: str) -> list[Action]:
    rows = db.execute(
        "SELECT * FROM actions WHERE call_id = ? ORDER BY created_at ASC",
        (call_id,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


def upsert_external_event(
    db: sqlite3.Connection,
    *,
    ics_uid: str,
    ics_url: str,
    title: str,
    start_at: int,
    end_at: int,
    all_day: bool = False,
    description: str | None = None,
    location: str | None = None,
) -> ExternalEvent:
    now = now_ms()
    existing = db.execute(
        "SELECT id, created_at FROM external_events WHERE ics_uid = ? AND ics_url = ?",
        (ics_uid, ics_url),
    ).fetchone()
    event_id = str(existing["id"]) if existing is not None else new_id()
    created_at = int(existing["created_at"]) if existing is not None else now
    with db:
        db.execute(
            """
            INSERT INTO external_events (
              id, ics_uid, ics_url, title, start_at, end_at, all_day, description, location,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ics_uid, ics_url) DO UPDATE SET
              title = excluded.title,
              start_at = excluded.start_at,
              end_at = excluded.end_at,
              all_day = excluded.all_day,
              description = excluded.description,
              location = excluded.location,
              updated_at = excluded.updated_at
            """,
            (
                event_id,
                ics_uid,
                ics_url,
                title,
                start_at,
                end_at,
                int(all_day),
                description,
                location,
                created_at,
                now,
            ),
        )
    stored = db.execute(
        "SELECT * FROM external_events WHERE ics_uid = ? AND ics_url = ?",
        (ics_uid, ics_url),
    ).fetchone()
    if stored is None:  # pragma: no cover - defensive.
        raise RuntimeError(f"External event upsert failed for {ics_uid}@{ics_url}")
    return cast(ExternalEvent, row_to_dataclass(stored, ExternalEvent))


def delete_stale_external_events(
    db: sqlite3.Connection,
    ics_url: str,
    current_uids: set[str],
) -> int:
    rows = db.execute(
        "SELECT ics_uid FROM external_events WHERE ics_url = ?",
        (ics_url,),
    ).fetchall()
    stale_uids = [str(row["ics_uid"]) for row in rows if str(row["ics_uid"]) not in current_uids]
    if not stale_uids:
        return 0
    placeholders = ", ".join("?" for _ in stale_uids)
    params: tuple[object, ...] = (ics_url, *stale_uids)
    with db:
        db.execute(
            f"DELETE FROM external_events WHERE ics_url = ? AND ics_uid IN ({placeholders})",
            params,
        )
    return len(stale_uids)


def get_external_events_in_range(
    db: sqlite3.Connection,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 200,
) -> list[ExternalEvent]:
    rows = db.execute(
        """
        SELECT * FROM external_events
        WHERE start_at < ? AND end_at > ?
        ORDER BY start_at ASC
        LIMIT ?
        """,
        (end_ms, start_ms, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, ExternalEvent)


def get_external_event_by_id(db: sqlite3.Connection, event_id: str) -> ExternalEvent | None:
    row = db.execute(
        "SELECT * FROM external_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    return row_to_dataclass(row, ExternalEvent)


def get_recent_external_events(db: sqlite3.Connection, limit: int = 100) -> list[ExternalEvent]:
    rows = db.execute(
        "SELECT * FROM external_events ORDER BY start_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, ExternalEvent)


def get_upcoming_external_events(
    db: sqlite3.Connection,
    *,
    within_ms: int,
    limit: int = 50,
) -> list[ExternalEvent]:
    now = now_ms()
    rows = db.execute(
        """
        SELECT * FROM external_events
        WHERE start_at >= ? AND start_at < ?
        ORDER BY start_at ASC
        LIMIT ?
        """,
        (now, now + within_ms, limit),
    ).fetchall()
    return rows_to_dataclasses(rows, ExternalEvent)


def get_actions_by_status(db: sqlite3.Connection, status: ActionStatus) -> list[Action]:
    rows = db.execute(
        "SELECT * FROM actions WHERE status = ? ORDER BY created_at DESC LIMIT 100",
        (status,),
    ).fetchall()
    return rows_to_dataclasses(rows, Action)


# ── active_calls ─────────────────────────────────────────────────────────────

def upsert_active_call(db: sqlite3.Connection, state: CallState) -> None:
    with db:
        db.execute(
            """
            INSERT INTO active_calls (
              call_id, person_id, person_name, audience, direction, channel, modality, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
              person_id = excluded.person_id,
              person_name = excluded.person_name,
              audience = excluded.audience,
              direction = excluded.direction,
              channel = excluded.channel,
              modality = excluded.modality,
              started_at = excluded.started_at,
              updated_at = excluded.updated_at
            """,
            (
                state.call_id,
                state.person_id,
                state.person_name,
                state.audience,
                state.direction,
                state.channel,
                state.modality,
                state.started_at,
                now_ms(),
            ),
        )


def get_active_call_by_id(db: sqlite3.Connection, call_id: str) -> CallState | None:
    row = db.execute(
        """
        SELECT active_calls.call_id, active_calls.person_id, active_calls.person_name,
               active_calls.audience, active_calls.direction, active_calls.channel,
               active_calls.modality,
               active_calls.started_at,
               calls.answered_at
        FROM active_calls
        LEFT JOIN calls ON calls.id = active_calls.call_id
        WHERE active_calls.call_id = ?
        """,
        (call_id,),
    ).fetchone()
    return _to_call_state(row)


def list_active_calls(db: sqlite3.Connection) -> list[CallState]:
    rows = db.execute(
        """
        SELECT active_calls.call_id, active_calls.person_id, active_calls.person_name,
               active_calls.audience, active_calls.direction, active_calls.channel,
               active_calls.modality,
               active_calls.started_at,
               calls.answered_at
        FROM active_calls
        LEFT JOIN calls ON calls.id = active_calls.call_id
        ORDER BY active_calls.started_at ASC
        """
    ).fetchall()
    states: list[CallState] = []
    for row in rows:
        state = _to_call_state(row)
        if state is not None:
            states.append(state)
    return states


def count_active_calls(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) AS count FROM active_calls").fetchone()
    if row is None:
        return 0
    return int(row["count"])


def delete_active_call(db: sqlite3.Connection, call_id: str) -> CallState | None:
    existing = get_active_call_by_id(db, call_id)
    if existing is None:
        return None
    with db:
        db.execute("DELETE FROM active_calls WHERE call_id = ?", (call_id,))
    return existing


def clear_active_calls(db: sqlite3.Connection) -> None:
    with db:
        db.execute("DELETE FROM active_calls")


def update_active_call_started_at(
    db: sqlite3.Connection,
    call_id: str,
    started_at: int,
) -> None:
    with db:
        db.execute(
            "UPDATE active_calls SET started_at = ?, updated_at = ? WHERE call_id = ?",
            (started_at, now_ms(), call_id),
        )


def touch_active_call(db: sqlite3.Connection, call_id: str) -> None:
    with db:
        db.execute(
            "UPDATE active_calls SET updated_at = ? WHERE call_id = ?",
            (now_ms(), call_id),
        )


def sweep_timed_out_active_calls(
    db: sqlite3.Connection,
    timeout_ms: int,
) -> list[CallState]:
    cutoff = now_ms() - timeout_ms
    rows = db.execute(
        """
        SELECT active_calls.call_id, active_calls.person_id, active_calls.person_name,
               active_calls.audience, active_calls.direction, active_calls.channel,
               active_calls.modality,
               active_calls.started_at,
               calls.answered_at
        FROM active_calls
        JOIN calls ON calls.id = active_calls.call_id
        WHERE active_calls.direction = 'outbound'
          AND active_calls.started_at < ?
          AND calls.ended_at IS NULL
          AND calls.answered_at IS NULL
        ORDER BY active_calls.started_at ASC
        """,
        (cutoff,),
    ).fetchall()
    states: list[CallState] = []
    for row in rows:
        state = _to_call_state(row)
        if state is not None:
            states.append(state)
    if not states:
        return []

    with db:
        for state in states:
            db.execute("DELETE FROM active_calls WHERE call_id = ?", (state.call_id,))
    return states


def prune_ended_active_calls(db: sqlite3.Connection) -> None:
    with db:
        db.execute(
            """
            DELETE FROM active_calls
            WHERE call_id IN (
              SELECT id FROM calls WHERE ended_at IS NOT NULL
            )
            """
        )


def _to_call_state(row: sqlite3.Row | None) -> CallState | None:
    if row is None:
        return None
    return CallState(
        call_id=str(row["call_id"]),
        person_id=str(row["person_id"]),
        person_name=row["person_name"],
        audience=row["audience"],
        direction=row["direction"],
        channel=row["channel"],
        modality=row["modality"],
        started_at=int(row["started_at"]),
        answered_at=row["answered_at"],
    )


# ── faq ──────────────────────────────────────────────────────────────────────

def upsert_faq_chunk(
    db: sqlite3.Connection,
    *,
    chunk_id: str,
    file_path: str,
    content: str,
    heading: str | None = None,
    embedding: Sequence[float] | bytes | bytearray | memoryview | None = None,
) -> FaqChunk:
    updated_at = now_ms()
    embedding_blob = pack_embedding(embedding)
    exists = db.execute("SELECT id FROM faq_chunks WHERE id = ?", (chunk_id,)).fetchone() is not None

    with db:
        if exists:
            db.execute(
                """
                UPDATE faq_chunks
                SET file_path = ?, heading = ?, content = ?, embedding = ?, updated_at = ?
                WHERE id = ?
                """,
                (file_path, heading, content, embedding_blob, updated_at, chunk_id),
            )
        else:
            db.execute(
                """
                INSERT INTO faq_chunks (id, file_path, heading, content, embedding, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, file_path, heading, content, embedding_blob, updated_at),
            )

        rowid = get_rowid(db, table="faq_chunks", id_column="id", id_value=chunk_id)
        if rowid is not None:
            delete_vec_rows(db, table="faq_vec", rowid_column="chunk_rowid", rowids=[rowid])
            if embedding_blob is not None:
                upsert_vec_row(
                    db,
                    table="faq_vec",
                    rowid_column="chunk_rowid",
                    rowid=rowid,
                    embedding=embedding_blob,
                )

    return FaqChunk(
        id=chunk_id,
        file_path=file_path,
        heading=heading,
        content=content,
        embedding=embedding_blob,
        updated_at=updated_at,
        )


def get_all_faq_chunks(db: sqlite3.Connection, limit: int = 100) -> list[FaqChunk]:
    rows = db.execute(
        "SELECT * FROM faq_chunks ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows_to_dataclasses(rows, FaqChunk)


def delete_faq_chunks_by_file(db: sqlite3.Connection, file_path: str) -> None:
    with db:
        rowids = [
            int(row["rowid"])
            for row in db.execute(
                "SELECT rowid FROM faq_chunks WHERE file_path = ?",
                (file_path,),
            ).fetchall()
        ]
        delete_vec_rows(db, table="faq_vec", rowid_column="chunk_rowid", rowids=rowids)
        db.execute("DELETE FROM faq_chunks WHERE file_path = ?", (file_path,))
