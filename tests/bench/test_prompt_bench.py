"""Benchmarks for owner prompt assembly with realistic, precomputed variables."""

from __future__ import annotations
from datetime import UTC, datetime

import pytest

from mystic.db import (
    close_database,
    initialize_schema,
    insert_action,
    insert_call,
    insert_fact,
    open_database,
    upsert_day_summary,
    update_action_status,
    update_call_summary,
    update_call_transcript,
    upsert_person,
)
from mystic.prompts import build_prompt, compute_variables
from mystic.types import CallState
from tests.python_helpers import TempAppHome, seed_core_files


def _seed_prompt_files(home) -> None:
    shared_dir = home / "prompts" / "shared"
    owner_dir = home / "prompts" / "owner"
    shared_dir.mkdir(parents=True, exist_ok=True)
    owner_dir.mkdir(parents=True, exist_ok=True)

    (shared_dir / "context.md").write_text(
        "\n".join(
            (
                "## Caller Snapshot",
                "Name: {{callerName}}",
                "Phone: {{callerPhone}}",
                "",
                "{{callerSummary}}",
                "",
                "Recent unprocessed conversation:",
                "{{verbatimRecentContext}}",
                "",
                "Recent days:",
                "{{recentDaysSummary}}",
            )
        ),
        encoding="utf-8",
    )
    (shared_dir / "faq.md").write_text(
        "\n".join(
            (
                "## Quick Reference",
                "- Business hours: {{businessHours}}",
                "- Current time: {{currentTime}} {{timezone}}",
                "- Tunnel URL: {{tunnelUrl}}",
            )
        ),
        encoding="utf-8",
    )
    (owner_dir / "briefing.md").write_text(
        "\n".join(
            (
                "## Owner Briefing",
                "{{#activeCalls}}Active calls:\n{{activeCalls}}{{/activeCalls}}",
                "{{#urgentItems}}Urgent items:\n{{urgentItems}}{{/urgentItems}}",
                "{{#pendingActions}}Pending actions:\n{{pendingActions}}{{/pendingActions}}",
                "{{#failedActions}}Failed actions:\n{{failedActions}}{{/failedActions}}",
            )
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def owner_prompt_variables():
    with TempAppHome() as home:
        seed_core_files(home)
        _seed_prompt_files(home)

        db = open_database(":memory:")
        initialize_schema(db)

        person = upsert_person(db, "+15550020001", "Avery")
        db.execute(
            "UPDATE people SET summary = ? WHERE id = ?",
            ("Longtime client who prefers concise scheduling updates.", person.id),
        )

        insert_fact(
            db,
            person_id=person.id,
            type="preference",
            content="Prefers Tuesday afternoon check-ins.",
            confidence=0.95,
            source="owner",
        )
        insert_fact(
            db,
            person_id=person.id,
            type="context",
            content="Needs the quarterly budget packet before review calls.",
            confidence=0.9,
            source="owner",
        )

        completed_call = insert_call(db, person_id=person.id, direction="inbound", audience="public")
        db.execute(
            "UPDATE calls SET started_at = ? WHERE id = ?",
            (int(datetime(2026, 3, 11, 15, 0, tzinfo=UTC).timestamp() * 1000), completed_call.id),
        )
        update_call_summary(db, completed_call.id, "Reviewed the budget packet and asked for a Tuesday follow-up.")
        update_call_transcript(
            db,
            completed_call.id,
            "[0:00] Caller: Do you have the budget packet?\n[0:04] Agent: Yes, I will follow up Tuesday.",
        )
        upsert_day_summary(
            db,
            person.id,
            "2026-03-10",
            "Reviewed the budget packet and asked for a Tuesday follow-up.",
        )

        pending_action = insert_action(
            db,
            person_id=person.id,
            intent="Call Avery with the updated budget packet",
            source="owner",
            urgency="high",
            due_at=int(datetime(2026, 3, 11, 16, 30, tzinfo=UTC).timestamp() * 1000),
        )
        failed_action = insert_action(
            db,
            person_id=person.id,
            intent="Send vendor onboarding summary",
            source="owner",
        )
        update_action_status(db, failed_action.id, "failed", "No answer on callback")

        variables = compute_variables(
            db,
            person,
            "owner",
            "outbound",
            {
                "active-1": CallState(
                    call_id="active-1",
                    person_id=person.id,
                    person_name=person.name,
                    audience="public",
                    direction="outbound",
                    channel="phone",
                    modality="voice",
                    started_at=int(datetime(2026, 3, 11, 16, 20, tzinfo=UTC).timestamp() * 1000),
                )
            },
            "https://bench.tail1234.ts.net",
            channel="phone",
            modality="voice",
            now=datetime(2026, 3, 11, 16, 25, tzinfo=UTC),
        )

        assert pending_action.intent in variables.pending_actions
        yield variables
        close_database(db)


@pytest.mark.bench
class TestPromptAssemblyBench:
    def test_build_owner_prompt(self, benchmark, owner_prompt_variables) -> None:
        benchmark(build_prompt, "owner", owner_prompt_variables)
