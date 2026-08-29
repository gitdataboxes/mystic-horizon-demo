from __future__ import annotations

import sqlite3
import unittest
from typing import cast
from unittest.mock import patch

from mystic.types import Audience, CallState, Direction
from mystic.db import clear_action_hub_event, delete_post_call_actions_by_call_id, get_action_by_id, get_actions_by_call_id, get_actions_by_status, get_actions_pending_hub_sync, get_all_pending_actions, get_due_actions, get_failed_actions, get_open_actions_by_person, get_pending_actions_by_person, increment_action_attempts, increment_hub_sync_attempts, insert_action, reset_action_to_pending, start_action_attempt, start_action_attempt, update_action_context, update_action_due_at, update_action_status, clear_active_calls, count_active_calls, delete_active_call, get_active_call_by_id, list_active_calls, prune_ended_active_calls, sweep_timed_out_active_calls, touch_active_call, update_active_call_started_at, upsert_active_call, append_call_transcript, clear_extraction_error, delete_call_by_id, delete_stale_external_events, get_call_by_external_id, get_call_by_id, get_calls_needing_extraction, get_external_events_in_range, get_in_progress_scheduled_actions, get_recent_calls, get_recent_calls_by_person, get_recent_summarized_calls_by_person, get_scheduled_actions_in_range, get_todays_calls, get_upcoming_external_events, get_upcoming_scheduled_actions, insert_call, mark_action_hub_failed, mark_action_hub_pending, mark_action_hub_synced, mark_commitments_extracted, mark_extraction_attempted, mark_extraction_error, mark_facts_extracted, update_action_time_slot, update_call_answered_at, update_call_end, update_call_external_id, update_call_summary, update_call_transcript, close_database, get_db_path, initialize_schema, open_database, bump_fact_confidence, delete_post_call_facts_by_call_id, get_active_facts_by_person, get_all_active_facts_by_person, get_facts_with_null_embeddings, insert_fact, supersede_fact, update_fact_embedding, delete_faq_chunks_by_file, upsert_external_event, upsert_faq_chunk, get_schema_version, run_migrations, write_initial_migration, find_people, get_person_by_id, get_person_by_phone, update_person_last_seen, update_person_name, update_person_summary, upsert_person, INITIAL_SCHEMA, delete_transcript_chunks_by_call_id, get_chunks_by_call_id, get_chunks_with_null_embeddings, insert_transcript_chunk, replace_transcript_chunks_for_call, update_chunk_embedding
from mystic.db import create_migration, get_applied_migrations
from tests.python_helpers import TempAppHome, make_embedding


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        self.db = open_database(":memory:")
        initialize_schema(self.db)

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def create_person(self, phone: str = "+15550001111", name: str | None = "Alice"):
        return upsert_person(self.db, phone, name)

    def create_call(
        self,
        *,
        person_id: str,
        direction: Direction = "inbound",
        audience: Audience = "public",
        external_id: str | None = None,
    ):
        return insert_call(
            self.db,
            person_id=person_id,
            direction=direction,
            audience=audience,
            external_id=external_id,
        )

    def count_rows(self, table: str) -> int:
        row = self.db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        self.assertIsNotNone(row)
        return int(row["count"])

    def fts_count(self, table: str, term: str) -> int:
        row = self.db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {table} MATCH ?",
            (term,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["count"])


class ConnectionAndMigrationTests(DatabaseTestCase):
    def test_schema_initialization_creates_tables_and_pragmas(self) -> None:
        foreign_keys = self.db.execute("PRAGMA foreign_keys").fetchone()
        self.assertIsNotNone(foreign_keys)
        self.assertEqual(int(foreign_keys[0]), 1)
        self.assertEqual(get_schema_version(self.db), 9)
        self.assertEqual(get_applied_migrations(self.db), ["001.sql", "002.sql", "003.sql", "004.sql", "005.sql", "006.sql", "007.sql", "008.sql", "009.sql"])

        table_names = {
            row["name"]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        self.assertIn("people", table_names)
        self.assertIn("calls", table_names)
        self.assertIn("external_events", table_names)
        self.assertIn("transcript_chunks_vec", table_names)
        call_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(calls)").fetchall()
        }
        self.assertIn("answered_at", call_columns)
        self.assertIn("last_extraction_attempt_at", call_columns)
        self.assertIn("channel", call_columns)
        self.assertIn("modality", call_columns)
        active_call_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(active_calls)").fetchall()
        }
        self.assertIn("channel", active_call_columns)
        self.assertIn("modality", active_call_columns)
        action_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(actions)").fetchall()
        }
        self.assertIn("start_at", action_columns)
        self.assertIn("end_at", action_columns)
        self.assertIn("hub_event_id", action_columns)
        self.assertIn("hub_sync_status", action_columns)
        self.assertIn("hub_sync_attempts", action_columns)
        self.assertEqual(get_db_path(), self.home / "mystic-horizon.db")

    def test_migrations_are_applied_from_disk_once(self) -> None:
        migration_file = create_migration("create migrated table")
        self.assertEqual(migration_file.name, "010_create_migrated_table.sql")
        migration_file.write_text("CREATE TABLE migrated (id TEXT PRIMARY KEY);\n", encoding="utf-8")

        self.assertEqual(run_migrations(self.db), 10)
        self.assertEqual(get_schema_version(self.db), 10)
        self.assertEqual(
            get_applied_migrations(self.db),
            ["001.sql", "002.sql", "003.sql", "004.sql", "005.sql", "006.sql", "007.sql", "008.sql", "009.sql", "010_create_migrated_table.sql"],
        )
        migrated = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migrated'"
        ).fetchone()
        self.assertIsNotNone(migrated)
        self.assertEqual(run_migrations(self.db), 10)

    def test_failed_migration_rolls_back_partial_changes(self) -> None:
        migration_file = create_migration("broken migration")
        migration_file.write_text(
            "CREATE TABLE partial_state (id TEXT PRIMARY KEY);\n"
            "INSERT INTO missing_table DEFAULT VALUES;\n",
            encoding="utf-8",
        )

        with self.assertRaises(sqlite3.Error):
            run_migrations(self.db)

        self.assertEqual(get_schema_version(self.db), 9)
        partial_state = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partial_state'"
        ).fetchone()
        self.assertIsNone(partial_state)

    def test_legacy_schema_is_upgraded_from_builtin_migrations(self) -> None:
        legacy_db = open_database(":memory:")
        try:
            write_initial_migration(INITIAL_SCHEMA)
            legacy_db.executescript(INITIAL_SCHEMA)
            legacy_db.commit()

            call_columns_before = {
                row["name"] for row in legacy_db.execute("PRAGMA table_info(calls)").fetchall()
            }
            self.assertNotIn("answered_at", call_columns_before)
            self.assertNotIn("last_extraction_attempt_at", call_columns_before)
            self.assertEqual(get_schema_version(legacy_db), 1)

            self.assertEqual(run_migrations(legacy_db), 9)
            self.assertEqual(get_schema_version(legacy_db), 9)
            call_columns_after = {
                row["name"] for row in legacy_db.execute("PRAGMA table_info(calls)").fetchall()
            }
            self.assertIn("answered_at", call_columns_after)
            self.assertIn("last_extraction_attempt_at", call_columns_after)
            self.assertIn("channel", call_columns_after)
            self.assertIn("modality", call_columns_after)
            self.assertNotIn("is_game_mode", call_columns_after)
            active_call_columns_after = {
                row["name"] for row in legacy_db.execute("PRAGMA table_info(active_calls)").fetchall()
            }
            self.assertIn("channel", active_call_columns_after)
            self.assertIn("modality", active_call_columns_after)
            action_columns_after = {
                row["name"] for row in legacy_db.execute("PRAGMA table_info(actions)").fetchall()
            }
            self.assertIn("start_at", action_columns_after)
            self.assertIn("end_at", action_columns_after)
            self.assertIn("hub_event_id", action_columns_after)
            self.assertIn("hub_sync_status", action_columns_after)
            self.assertIn("hub_sync_attempts", action_columns_after)
            self.assertIsNotNone(
                legacy_db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'external_events'"
                ).fetchone()
            )
        finally:
            close_database(legacy_db)

    def test_existing_columns_are_marked_applied_without_rerunning_migrations(self) -> None:
        drifted_db = open_database(":memory:")
        try:
            write_initial_migration(INITIAL_SCHEMA)
            drifted_db.executescript(INITIAL_SCHEMA)
            drifted_db.execute("ALTER TABLE calls ADD COLUMN answered_at INTEGER")
            drifted_db.execute("ALTER TABLE calls ADD COLUMN last_extraction_attempt_at INTEGER")
            drifted_db.execute("ALTER TABLE actions ADD COLUMN start_at INTEGER")
            drifted_db.execute("ALTER TABLE actions ADD COLUMN end_at INTEGER")
            drifted_db.executescript(
                """
                CREATE TABLE external_events (
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
                """
            )
            drifted_db.execute("ALTER TABLE actions ADD COLUMN hub_event_id TEXT")
            drifted_db.execute("ALTER TABLE actions ADD COLUMN hub_sync_status TEXT")
            drifted_db.execute("ALTER TABLE actions ADD COLUMN hub_sync_attempts INTEGER NOT NULL DEFAULT 0")
            drifted_db.commit()

            self.assertEqual(get_schema_version(drifted_db), 1)
            self.assertEqual(run_migrations(drifted_db), 9)
            self.assertEqual(get_applied_migrations(drifted_db), ["001.sql", "002.sql", "003.sql", "004.sql", "005.sql", "006.sql", "007.sql", "008.sql", "009.sql"])
            self.assertEqual(run_migrations(drifted_db), 9)
        finally:
            close_database(drifted_db)


class PeopleTests(DatabaseTestCase):
    def test_people_crud_and_search(self) -> None:
        person = upsert_person(self.db, "+15550001111")
        original_first_seen = person.first_seen
        updated = upsert_person(self.db, "+15550001111", "Alice")
        self.assertEqual(updated.id, person.id)
        self.assertEqual(updated.name, "Alice")
        self.assertGreaterEqual(updated.last_seen, person.last_seen)
        self.assertEqual(updated.first_seen, original_first_seen)

        rename = update_person_name(self.db, "+15550001111", "Alice Smith")
        self.assertIsNotNone(rename)
        assert rename is not None
        self.assertEqual(rename.name, "Alice Smith")

        update_person_summary(self.db, person.id, "Prefers concise updates.")
        update_person_last_seen(self.db, person.id)
        by_id = get_person_by_id(self.db, person.id)
        by_phone = get_person_by_phone(self.db, "+15550001111")
        self.assertIsNotNone(by_id)
        self.assertIsNotNone(by_phone)
        assert by_id is not None
        assert by_phone is not None
        self.assertEqual(by_id.summary, "Prefers concise updates.")
        self.assertEqual(by_phone.name, "Alice Smith")

        other = upsert_person(self.db, "+15550002222", "Bob")
        matches = find_people(self.db, "Alice")
        self.assertEqual([match.id for match in matches], [person.id])
        self.assertIsNotNone(get_person_by_id(self.db, other.id))


class CallTests(DatabaseTestCase):
    def test_call_crud_and_extraction_queries(self) -> None:
        person = self.create_person()
        call = self.create_call(person_id=person.id)

        update_call_external_id(self.db, call.id, "CA123")
        external = get_call_by_external_id(self.db, "CA123")
        self.assertIsNotNone(external)
        assert external is not None
        self.assertEqual(external.id, call.id)

        update_call_transcript(self.db, call.id, "Hello")
        append_call_transcript(self.db, call.id, "Need a follow-up")
        update_call_answered_at(self.db, call.id, 123)
        update_call_answered_at(self.db, call.id, 456)
        mark_extraction_attempted(self.db, call.id, 789)
        mark_extraction_error(self.db, call.id, "boom")

        stored = get_call_by_id(self.db, call.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.answered_at, 123)
        self.assertIsNotNone(stored.last_extraction_attempt_at)
        self.assertIn("Need a follow-up", stored.transcript or "")
        self.assertEqual(stored.extraction_retries, 1)
        self.assertEqual(stored.extraction_error, "boom")

        needs = get_calls_needing_extraction(self.db)
        self.assertEqual([item.id for item in needs], [call.id])

        replace_transcript_chunks_for_call(
            self.db,
            call.id,
            person.id,
            [{"content": "Hello"}, {"content": "Need a follow-up"}],
        )
        update_call_summary(self.db, call.id, "Caller requested a follow-up.")
        mark_facts_extracted(self.db, call.id)
        mark_commitments_extracted(self.db, call.id)
        clear_extraction_error(self.db, call.id)
        update_call_end(self.db, call.id, duration=42)

        stored = get_call_by_id(self.db, call.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.duration, 42)
        self.assertEqual(stored.summary, "Caller requested a follow-up.")
        self.assertIsNone(stored.extraction_error)
        self.assertIsNone(stored.last_extraction_attempt_at)
        self.assertEqual(get_calls_needing_extraction(self.db), [])

        self.assertEqual([item.id for item in get_recent_calls_by_person(self.db, person.id)], [call.id])
        self.assertEqual(
            [item.id for item in get_recent_summarized_calls_by_person(self.db, person.id)],
            [call.id],
        )
        self.assertEqual([item.id for item in get_todays_calls(self.db)], [call.id])

        delete_transcript_chunks_by_call_id(self.db, call.id)
        delete_call_by_id(self.db, call.id)
        self.assertIsNone(get_call_by_id(self.db, call.id))

class ActiveCallTests(DatabaseTestCase):
    def test_active_call_lifecycle(self) -> None:
        person = self.create_person()
        call = self.create_call(person_id=person.id, direction="outbound")
        state = CallState(
            call_id=call.id,
            person_id=person.id,
            person_name="Alice",
            audience="public",
            direction="outbound",
            channel="phone",
            modality="voice",
            started_at=call.started_at,
        )

        upsert_active_call(self.db, state)
        update_active_call_started_at(self.db, call.id, 111)
        touch_active_call(self.db, call.id)

        fetched = get_active_call_by_id(self.db, call.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.started_at, 111)
        self.assertEqual(count_active_calls(self.db), 1)
        self.assertEqual([item.call_id for item in list_active_calls(self.db)], [call.id])

        deleted = delete_active_call(self.db, call.id)
        self.assertIsNotNone(deleted)
        assert deleted is not None
        self.assertEqual(deleted.call_id, call.id)
        self.assertIsNone(get_active_call_by_id(self.db, call.id))

        upsert_active_call(self.db, state)
        clear_active_calls(self.db)
        self.assertEqual(count_active_calls(self.db), 0)

    def test_timeout_sweep_and_prune_only_remove_expected_calls(self) -> None:
        person = self.create_person()

        timed_out = self.create_call(person_id=person.id, direction="outbound")
        answered = self.create_call(person_id=person.id, direction="outbound")
        inbound = self.create_call(person_id=person.id, direction="inbound")

        for call, direction in (
            (timed_out, "outbound"),
            (answered, "outbound"),
            (inbound, "inbound"),
        ):
            upsert_active_call(
                self.db,
                CallState(
                    call_id=call.id,
                    person_id=person.id,
                    person_name="Alice",
                    audience="public",
                    direction=cast(Direction, direction),
                    channel="phone",
                    modality="voice",
                    started_at=call.started_at,
                ),
            )
            update_active_call_started_at(self.db, call.id, 1)

        update_call_answered_at(self.db, answered.id, 5)
        swept = sweep_timed_out_active_calls(self.db, timeout_ms=1)
        self.assertEqual([item.call_id for item in swept], [timed_out.id])
        self.assertIsNone(get_active_call_by_id(self.db, timed_out.id))
        self.assertIsNotNone(get_active_call_by_id(self.db, answered.id))
        self.assertIsNotNone(get_active_call_by_id(self.db, inbound.id))

        update_call_end(self.db, answered.id, ended_at=10)
        prune_ended_active_calls(self.db)
        self.assertIsNone(get_active_call_by_id(self.db, answered.id))
        self.assertIsNotNone(get_active_call_by_id(self.db, inbound.id))


class TranscriptChunkTests(DatabaseTestCase):
    def test_transcript_chunks_manage_fts_and_vec_rows(self) -> None:
        person = self.create_person()
        call = self.create_call(person_id=person.id)

        chunk = insert_transcript_chunk(
            self.db,
            call_id=call.id,
            person_id=person.id,
            content="alpha galaxy",
            chunk_index=0,
            embedding=make_embedding([0.1, 0.2, 0.3]),
        )
        self.assertEqual(self.fts_count("transcript_chunks_fts", "galaxy"), 1)
        self.assertEqual(self.count_rows("transcript_chunks_vec"), 1)

        update_chunk_embedding(self.db, chunk.id, make_embedding([0.4, 0.5, 0.6]))
        refreshed = get_chunks_by_call_id(self.db, call.id)
        self.assertEqual(len(refreshed), 1)
        self.assertIsNotNone(refreshed[0].embedding)

        replace_transcript_chunks_for_call(
            self.db,
            call.id,
            person.id,
            [
                {"content": "beta nebula"},
                {"content": "gamma signal", "embedding": make_embedding([0.7, 0.8])},
            ],
        )
        self.assertEqual(self.fts_count("transcript_chunks_fts", "galaxy"), 0)
        self.assertEqual(self.fts_count("transcript_chunks_fts", "nebula"), 1)
        self.assertEqual(len(get_chunks_by_call_id(self.db, call.id)), 2)
        self.assertEqual(len(get_chunks_with_null_embeddings(self.db)), 1)

        delete_transcript_chunks_by_call_id(self.db, call.id)
        self.assertEqual(self.count_rows("transcript_chunks"), 0)
        self.assertEqual(self.count_rows("transcript_chunks_vec"), 0)


class FactTests(DatabaseTestCase):
    def test_facts_manage_embeddings_confidence_and_fts(self) -> None:
        person = self.create_person()
        call = self.create_call(person_id=person.id)

        active = insert_fact(
            self.db,
            person_id=person.id,
            call_id=call.id,
            source_text="Caller loves tea",
            type="preference",
            content="Likes green tea",
            confidence=0.4,
            source="caller",
            embedding=make_embedding([0.1, 0.2]),
        )
        post_call = insert_fact(
            self.db,
            person_id=person.id,
            call_id=call.id,
            type="context",
            content="Needs a callback tomorrow",
            confidence=0.6,
            source="post-call",
        )
        self.assertEqual(self.fts_count("facts_fts", "green"), 1)
        self.assertEqual(self.count_rows("facts_vec"), 1)

        bump_fact_confidence(self.db, active.id, 0.9)
        update_fact_embedding(self.db, post_call.id, make_embedding([0.3, 0.4]))
        all_active = get_all_active_facts_by_person(self.db, person.id)
        self.assertEqual(len(all_active), 2)
        self.assertGreaterEqual(all_active[0].confidence, all_active[1].confidence)
        self.assertEqual(len(get_facts_with_null_embeddings(self.db)), 0)

        supersede_fact(self.db, active.id)
        self.assertEqual(self.fts_count("facts_fts", "green"), 0)
        self.assertEqual([fact.id for fact in get_active_facts_by_person(self.db, person.id)], [post_call.id])

        delete_post_call_facts_by_call_id(self.db, call.id)
        remaining = get_all_active_facts_by_person(self.db, person.id)
        self.assertEqual(remaining, [])
        self.assertEqual(self.count_rows("facts_vec"), 0)


class ActionTests(DatabaseTestCase):
    def test_actions_queries_and_status_updates(self) -> None:
        person = self.create_person()
        call = self.create_call(person_id=person.id)
        now = 1

        urgent = insert_action(
            self.db,
            person_id=person.id,
            call_id=call.id,
            intent="Call back urgently",
            urgency="high",
            source="post-call",
        )
        scheduled = insert_action(
            self.db,
            person_id=person.id,
            call_id=call.id,
            intent="Send recap",
            due_at=now,
            source="post-call",
        )
        blocked = insert_action(
            self.db,
            intent="Should not run",
            due_at=now,
            source="owner",
        )
        self.db.execute(
            "UPDATE actions SET attempts = max_attempts WHERE id = ?",
            (blocked.id,),
        )
        self.db.commit()

        due = get_due_actions(self.db)
        self.assertEqual([item.id for item in due[:2]], [urgent.id, scheduled.id])
        self.assertNotIn(blocked.id, [item.id for item in due])
        self.assertEqual([item.id for item in get_pending_actions_by_person(self.db, person.id)], [urgent.id, scheduled.id])

        increment_action_attempts(self.db, scheduled.id)
        start_action_attempt(self.db, urgent.id)
        update_action_context(self.db, urgent.id, "Caller asked for urgency.")
        update_action_due_at(self.db, urgent.id, 500)
        self.assertEqual(
            [item.id for item in get_open_actions_by_person(self.db, person.id)],
            [scheduled.id, urgent.id],
        )

        update_action_status(self.db, urgent.id, "failed", "line busy")
        failed = get_failed_actions(self.db)
        self.assertEqual([item.id for item in failed], [urgent.id])
        self.assertEqual([item.id for item in get_actions_by_status(self.db, "failed")], [urgent.id])

        reset_action_to_pending(self.db, urgent.id, 700, "retry later")
        pending = get_all_pending_actions(self.db)
        self.assertIn(urgent.id, [item.id for item in pending])
        by_call = get_actions_by_call_id(self.db, call.id)
        self.assertEqual([item.id for item in by_call], [urgent.id, scheduled.id])

        delete_post_call_actions_by_call_id(self.db, call.id)
        self.assertEqual(get_actions_by_call_id(self.db, call.id), [])
        self.assertIsNotNone(get_action_by_id(self.db, blocked.id))

    def test_scheduled_action_and_external_event_queries(self) -> None:
        person = self.create_person()
        action = insert_action(
            self.db,
            person_id=person.id,
            intent="Scheduled callback",
            source="owner",
            start_at=100,
            end_at=200,
        )
        self.assertEqual(action.due_at, 100)

        update_action_time_slot(self.db, action.id, 150, 250)
        stored_action = get_action_by_id(self.db, action.id)
        assert stored_action is not None
        self.assertEqual(stored_action.start_at, 150)
        self.assertEqual(stored_action.end_at, 250)

        event = upsert_external_event(
            self.db,
            ics_uid="evt-1",
            ics_url="https://example.test/work.ics",
            title="Work block",
            start_at=175,
            end_at=300,
        )
        self.assertEqual(event.title, "Work block")

        self.assertEqual([item.id for item in get_scheduled_actions_in_range(self.db, 200, 225)], [action.id])
        self.assertEqual([item.id for item in get_in_progress_scheduled_actions(self.db)], [])

        with patch("mystic.db.now_ms", return_value=160):
            self.assertEqual([item.id for item in get_in_progress_scheduled_actions(self.db)], [action.id])
            self.assertEqual([item.id for item in get_upcoming_scheduled_actions(self.db, within_ms=200)], [])
            upcoming_events = get_upcoming_external_events(self.db, within_ms=200)
            self.assertEqual([item.id for item in upcoming_events], [event.id])

        ranged_events = get_external_events_in_range(self.db, 200, 225)
        self.assertEqual([item.id for item in ranged_events], [event.id])
        self.assertEqual(delete_stale_external_events(self.db, "https://example.test/work.ics", set()), 1)
        self.assertEqual(get_external_events_in_range(self.db, 0, 500), [])

    def test_hub_sync_fields_round_trip_and_helpers(self) -> None:
        person = self.create_person()
        action = insert_action(
            self.db,
            person_id=person.id,
            intent="Hub-backed appointment",
            source="owner",
            start_at=100,
            end_at=200,
            hub_sync_status="pending",
        )

        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.hub_sync_status, "pending")
        self.assertEqual(stored.hub_sync_attempts, 0)
        self.assertEqual([item.id for item in get_actions_pending_hub_sync(self.db)], [action.id])

        attempts = increment_hub_sync_attempts(self.db, action.id)
        self.assertEqual(attempts, 1)

        mark_action_hub_pending(self.db, action.id)
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.hub_sync_attempts, 0)
        self.assertEqual(stored.hub_sync_status, "pending")

        mark_action_hub_synced(self.db, action.id, "evt-123")
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.hub_event_id, "evt-123")
        self.assertEqual(stored.hub_sync_status, "synced")
        self.assertEqual(stored.hub_sync_attempts, 0)

        update_action_time_slot(self.db, action.id, 150, 250, hub_sync_status="pending")
        update_action_status(self.db, action.id, "cancelled", "cancelled", hub_sync_status="pending")
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.start_at, 150)
        self.assertEqual(stored.end_at, 250)
        self.assertEqual(stored.hub_sync_status, "pending")

        mark_action_hub_failed(self.db, action.id)
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.hub_sync_status, "failed")

        clear_action_hub_event(self.db, action.id)
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertIsNone(stored.hub_event_id)
        self.assertIsNone(stored.hub_sync_status)
        self.assertEqual(stored.hub_sync_attempts, 0)


class FaqTests(DatabaseTestCase):
    def test_faq_chunks_update_fts_and_vec_rows(self) -> None:
        upsert_faq_chunk(
            self.db,
            chunk_id="faq-1",
            file_path="docs/faq.md",
            heading="Greeting",
            content="hello from the handbook",
            embedding=make_embedding([0.1, 0.2]),
        )
        self.assertEqual(self.fts_count("faq_fts", "handbook"), 1)
        self.assertEqual(self.count_rows("faq_vec"), 1)

        updated = upsert_faq_chunk(
            self.db,
            chunk_id="faq-1",
            file_path="docs/faq.md",
            heading="Greeting",
            content="goodbye from the handbook",
        )
        self.assertEqual(updated.content, "goodbye from the handbook")
        self.assertEqual(self.fts_count("faq_fts", "hello"), 0)
        self.assertEqual(self.fts_count("faq_fts", "goodbye"), 1)
        self.assertEqual(self.count_rows("faq_vec"), 0)

        delete_faq_chunks_by_file(self.db, "docs/faq.md")
        self.assertEqual(self.count_rows("faq_chunks"), 0)
        self.assertEqual(self.count_rows("faq_vec"), 0)


if __name__ == "__main__":
    unittest.main()
