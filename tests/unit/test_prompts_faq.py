from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from mystic.types import CallState, PromptVariables, SearchResult
from mystic.config import clear_config_cache
from mystic.db import (
    insert_action,
    insert_call,
    insert_fact,
    mark_day_extraction_complete,
    upsert_day_summary,
    upsert_external_event,
    update_action_status,
    update_call_summary,
    update_call_transcript,
    close_database,
    initialize_schema,
    open_database,
    upsert_person,
)
from mystic.memory import index_faq_files, search_faq
from mystic.prompts import build_prompt, compute_variables, render
from tests.python_helpers import TempAppHome, make_embedding, seed_core_files


class PromptRenderingTests(unittest.TestCase):
    def test_render_interpolates_variables_and_sections(self) -> None:
        template = "{{greeting}} {{name}} {{#enabled}}YES{{/enabled}}{{^enabled}}NO{{/enabled}}"
        result = render(
            template,
            {"greeting": "Hello", "name": "Ada", "enabled": True},
        )
        self.assertEqual(result, "Hello Ada YES")


class PromptAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def test_compute_variables_builds_owner_context(self) -> None:
        person = upsert_person(self.db, "+15550001111", "Alice")
        insert_fact(
            self.db,
            person_id=person.id,
            type="context",
            content="Prefers text reminders.",
            confidence=0.9,
            source="owner",
        )
        self.db.execute(
            "UPDATE people SET summary = ? WHERE id = ?",
            ("Friendly long-term client.", person.id),
        )

        call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        started_at = int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000)
        self.db.execute("UPDATE calls SET started_at = ? WHERE id = ?", (started_at, call.id))
        update_call_summary(self.db, call.id, "Asked for a Tuesday follow-up.")
        update_call_transcript(
            self.db,
            call.id,
            "[0:00] Caller: Hi\n[0:05] Agent: Let's follow up on Tuesday.",
        )
        previous_summary = upsert_day_summary(
            self.db,
            person.id,
            "2026-03-10",
            "Asked for a Tuesday follow-up.",
        )
        mark_day_extraction_complete(self.db, previous_summary.id)

        insert_action(
            self.db,
            person_id=person.id,
            intent="Call Alice back",
            source="owner",
            urgency="high",
            due_at=int(datetime(2026, 3, 11, 16, 5, tzinfo=UTC).timestamp() * 1000),
        )
        insert_action(
            self.db,
            person_id=person.id,
            intent="Alice appointment",
            source="owner",
            start_at=int(datetime(2026, 3, 11, 16, 15, tzinfo=UTC).timestamp() * 1000),
            end_at=int(datetime(2026, 3, 11, 16, 45, tzinfo=UTC).timestamp() * 1000),
        )
        upsert_external_event(
            self.db,
            ics_uid="evt-1",
            ics_url="https://example.test/work.ics",
            title="Team meeting",
            start_at=int(datetime(2026, 3, 11, 15, 30, tzinfo=UTC).timestamp() * 1000),
            end_at=int(datetime(2026, 3, 11, 16, 30, tzinfo=UTC).timestamp() * 1000),
        )
        failed_action = insert_action(
            self.db,
            person_id=person.id,
            intent="Send brochure",
            source="owner",
        )
        update_action_status(self.db, failed_action.id, "failed", "No answer")

        variables = compute_variables(
            self.db,
            person,
            "owner",
            "inbound",
            {
                "active-1": CallState(
                    call_id="active-1",
                    person_id=person.id,
                    person_name="Alice",
                    audience="public",
                    direction="outbound",
                    channel="phone",
                    modality="voice",
                    started_at=int(datetime(2026, 3, 11, 16, 2, 55, tzinfo=UTC).timestamp() * 1000),
                )
            },
            "https://example.test",
            channel="phone",
            modality="voice",
            now=datetime(2026, 3, 11, 16, 5, 0, tzinfo=UTC),
        )

        self.assertEqual(variables.current_time, "12:05 PM")
        self.assertEqual(variables.day_of_week, "Wednesday")
        self.assertEqual(variables.full_date, "March 11, 2026")
        self.assertEqual(
            variables.business_hours,
            "9:00-17:00 America/New_York (Monday, Tuesday, Wednesday, Thursday, Friday)",
        )
        self.assertEqual(variables.caller_name, "Alice")
        self.assertIn("Friendly long-term client.", variables.caller_summary)
        self.assertIn("Prefers text reminders.", variables.caller_summary)
        self.assertIn("2026-03-10: Asked for a Tuesday follow-up.", variables.recent_days_summary)
        self.assertIn("Caller: Hi", variables.verbatim_recent_context)
        self.assertIn("Call Alice back", variables.pending_actions)
        self.assertIn("3/11/2026, 12:05:00 PM", variables.pending_actions)
        self.assertIn("[URGENT] Call Alice back", variables.urgent_items)
        self.assertIn("Send brochure", variables.failed_actions)
        self.assertEqual(variables.direction, "Inbound")
        self.assertEqual(variables.channel_label, "Phone")
        self.assertEqual(variables.modality, "Voice")
        self.assertIn("Alice (Outbound phone call, 2m 5s)", variables.active_calls)
        self.assertIn("Team meeting", variables.current_schedule)
        self.assertIn("Alice appointment", variables.upcoming_schedule)
        self.assertEqual(variables.tunnel_url, "https://example.test")
        self.assertEqual(variables.webhook_secret, "")

    def test_compute_variables_guides_owner_to_phone_setup_skills(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")
        clear_config_cache("providers.json")

        person = upsert_person(self.db, "+15550001111", "Alice")
        variables = compute_variables(
            self.db,
            person,
            "owner",
            "inbound",
            {},
            channel="dashboard",
            modality="text",
            now=datetime(2026, 3, 11, 16, 5, 0, tzinfo=UTC),
        )

        self.assertIn("help directly", variables.phone_setup_hint)
        self.assertIn("write-twilio-credentials", variables.phone_setup_hint)
        self.assertIn("read-twilio-numbers", variables.phone_setup_hint)
        self.assertIn("write-twilio-number", variables.phone_setup_hint)
        self.assertNotIn("configure it on the Settings page", variables.phone_setup_hint)

    def test_compute_variables_keeps_unfinalized_prior_days_verbatim(self) -> None:
        person = upsert_person(self.db, "+15550002222", "Blair")

        finalized_call = insert_call(self.db, person_id=person.id, direction="inbound", audience="public")
        finalized_started = int(datetime(2026, 3, 10, 15, 0, tzinfo=UTC).timestamp() * 1000)
        self.db.execute(
            "UPDATE calls SET started_at = ? WHERE id = ?",
            (finalized_started, finalized_call.id),
        )
        update_call_transcript(
            self.db,
            finalized_call.id,
            "[0:00] Caller: This finalized transcript should stay summarized.",
        )
        finalized_summary = upsert_day_summary(
            self.db,
            person.id,
            "2026-03-10",
            "Handled the finalized planning conversation.",
        )
        mark_day_extraction_complete(self.db, finalized_summary.id)

        unfinalized_call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            channel="dashboard",
            modality="text",
            audience="owner",
        )
        unfinalized_started = int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000)
        self.db.execute(
            "UPDATE calls SET started_at = ? WHERE id = ?",
            (unfinalized_started, unfinalized_call.id),
        )
        update_call_transcript(
            self.db,
            unfinalized_call.id,
            "[0:00] Caller [text]: Yesterday's exact wording still matters.",
        )

        variables = compute_variables(
            self.db,
            person,
            "owner",
            "inbound",
            {},
            channel="dashboard",
            modality="text",
            now=datetime(2026, 3, 12, 5, 30, tzinfo=UTC),
        )

        self.assertIn("Yesterday's exact wording still matters.", variables.verbatim_recent_context)
        self.assertIn("2026-03-11 11:00 AM Dashboard chat", variables.verbatim_recent_context)
        self.assertNotIn(
            "This finalized transcript should stay summarized",
            variables.verbatim_recent_context,
        )
        self.assertIn(
            "2026-03-10: Handled the finalized planning conversation.",
            variables.recent_days_summary,
        )

    def test_build_prompt_renders_identity_soul_and_prompt_segments(self) -> None:
        shared_dir = self.home / "prompts" / "shared"
        owner_dir = self.home / "prompts" / "owner"
        shared_dir.mkdir(parents=True, exist_ok=True)
        owner_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "context.md").write_text("Caller: {{callerName}}", encoding="utf-8")
        (owner_dir / "briefing.md").write_text(
            "{{#pendingActions}}Pending: {{pendingActions}}{{/pendingActions}}",
            encoding="utf-8",
        )
        (owner_dir / "skills.md").write_text("Tools ready.", encoding="utf-8")

        prompt = build_prompt(
            "owner",
            PromptVariables(
                caller_name="Alice",
                pending_actions="- Call Alice back",
            ),
        )

        self.assertIn("# Identity", prompt)
        self.assertIn("# Test Soul", prompt)
        self.assertIn("Caller: Alice", prompt)
        self.assertIn("Pending: - Call Alice back", prompt)
        self.assertIn("Tools ready.", prompt)
        self.assertLess(prompt.index("# Identity"), prompt.index("# Test Soul"))
        self.assertLess(prompt.index("Caller: Alice"), prompt.index("Pending: - Call Alice back"))


class FaqTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_index_faq_files_upserts_chunks(self) -> None:
        faq_dir = self.home / "faq"
        faq_dir.mkdir(parents=True, exist_ok=True)
        faq_file = faq_dir / "hours.md"
        faq_file.write_text("## Office Hours\n\nWe are open weekdays.", encoding="utf-8")

        with patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(return_value=[make_embedding([1.0, 0.0])]),
        ):
            indexed = await index_faq_files(self.db)

        self.assertEqual(indexed, 1)
        row = self.db.execute(
            "SELECT id, file_path, heading, content FROM faq_chunks"
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], "faq-hours.md-0")
        self.assertEqual(row["file_path"], "hours.md")
        self.assertEqual(row["heading"], "Office Hours")
        self.assertIn("We are open weekdays.", row["content"])

        faq_file.write_text("## Office Hours\n\nWe are open every day.", encoding="utf-8")
        with patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(return_value=[make_embedding([0.0, 1.0])]),
        ):
            reindexed = await index_faq_files(self.db)

        self.assertEqual(reindexed, 1)
        row_count = self.db.execute("SELECT COUNT(*) AS count FROM faq_chunks").fetchone()
        self.assertIsNotNone(row_count)
        assert row_count is not None
        self.assertEqual(int(row_count["count"]), 1)
        updated = self.db.execute("SELECT content FROM faq_chunks WHERE id = ?", ("faq-hours.md-0",)).fetchone()
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIn("open every day", str(updated["content"]))

    async def test_search_faq_delegates_to_hybrid_search(self) -> None:
        expected = [SearchResult(id="faq-1", content="Answer", score=0.9)]
        with patch(
            "mystic.memory.hybrid_search",
            new=AsyncMock(return_value=expected),
        ) as mock_search:
            results = await search_faq(self.db, "hours", limit=2)

        self.assertEqual(results, expected)
        mock_search.assert_awaited_once_with(self.db, "faq", "hours", None, 2)


if __name__ == "__main__":
    unittest.main()
