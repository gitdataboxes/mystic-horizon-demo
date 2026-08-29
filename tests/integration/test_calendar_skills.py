from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from mystic.config import clear_config_cache
from mystic.db import get_action_by_id, insert_action, insert_call, upsert_external_event, upsert_person
from mystic.skills import execute_tool_calls, get_registry, load_handler_module
from mystic.types import SkillContext
from tests.integration.helpers import ALT_PHONE, PUBLIC_PHONE


def _configure_hub(integration_env) -> None:
    providers_path = integration_env.home / "config" / "providers.json"
    payload = json.loads(providers_path.read_text(encoding="utf-8"))
    payload["calendar"] = {
        "hub": {
            "provider": "caldav",
            "calendarId": "/dav/cal/default/",
            "baseUrl": "https://nextcloud.example.test",
            "username": "alice",
            "password": "secret",
            "writeEnabled": True,
        }
    }
    providers_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    clear_config_cache("providers.json")


async def test_calendar_read_and_availability_skills_are_audience_scoped(
    integration_env,
) -> None:
    alice = upsert_person(integration_env.db, PUBLIC_PHONE, "Alice")
    bob = upsert_person(integration_env.db, ALT_PHONE, "Bob")
    insert_action(
        integration_env.db,
        person_id=alice.id,
        intent="Alice callback",
        source="owner",
        start_at=1_775_055_600_000,
        end_at=1_775_057_400_000,
    )
    insert_action(
        integration_env.db,
        person_id=bob.id,
        intent="Bob callback",
        source="owner",
        start_at=1_775_059_200_000,
        end_at=1_775_061_000_000,
    )
    upsert_external_event(
        integration_env.db,
        ics_uid="evt-1",
        ics_url="https://example.test/work.ics",
        title="Team meeting",
        start_at=1_775_051_200_000,
        end_at=1_775_054_800_000,
    )

    with patch("mystic.db.now_ms", return_value=1_775_000_000_000):
        owner_results = await execute_tool_calls(
            integration_env.db,
            SkillContext(
                audience="owner",
                direction="inbound",
                channel="cli",
                modality="text",
                call_id="call-owner",
                person_id=alice.id,
                source="cli",
            ),
            [
                {"id": "read-calendar", "function": {"name": "read-calendar", "arguments": {}}},
                {
                    "id": "check-availability-owner",
                    "function": {
                        "name": "check-availability",
                        "arguments": {
                            "start": "2026-04-01T14:00:00Z",
                            "end": "2026-04-01T14:15:00Z",
                        },
                    },
                },
            ],
        )
        public_results = await execute_tool_calls(
            integration_env.db,
            SkillContext(
                audience="public",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id="call-public",
                person_id=alice.id,
                source="mid-call",
            ),
            [
                {"id": "read-calendar-public", "function": {"name": "read-calendar", "arguments": {}}},
                {
                    "id": "check-availability-public",
                    "function": {
                        "name": "check-availability",
                        "arguments": {
                            "start": "2026-04-01T14:00:00Z",
                            "end": "2026-04-01T14:15:00Z",
                        },
                    },
                },
                {
                    "id": "slots-public",
                    "function": {
                        "name": "find-open-slots",
                        "arguments": {
                            "start": "2026-04-01T13:00:00Z",
                            "end": "2026-04-01T17:00:00Z",
                            "min_duration_minutes": 30,
                        },
                    },
                },
            ],
        )

    assert "Team meeting" in owner_results[0].result
    assert "Bob callback" in owner_results[0].result
    assert "Team meeting" in owner_results[1].result

    assert "Alice callback" in public_results[0].result
    assert "Team meeting" not in public_results[0].result
    assert "Bob callback" not in public_results[0].result
    assert public_results[1].result == "That time is already booked."
    assert "Open slots:" in public_results[2].result


async def test_public_can_book_and_manage_own_appointment(
    integration_env,
) -> None:
    _configure_hub(integration_env)
    person = upsert_person(integration_env.db, PUBLIC_PHONE, "Alice")
    call = insert_call(
        integration_env.db,
        person_id=person.id,
        direction="inbound",
        audience="public",
    )
    write_action_module = load_handler_module(get_registry()["write-action"])
    manage_appointment_module = load_handler_module(get_registry()["manage-appointment"])

    with patch.object(write_action_module, "create_hub_event", new=AsyncMock(return_value=True)) as create_hub_event:
        created = await execute_tool_calls(
            integration_env.db,
            SkillContext(
                audience="public",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id=call.id,
                person_id=person.id,
                source="mid-call",
            ),
            [
                {
                    "id": "write-action",
                    "function": {
                        "name": "write-action",
                        "arguments": {
                            "intent": "Schedule callback",
                            "start_at": "2026-04-01T14:00:00Z",
                            "end_at": "2026-04-01T14:30:00Z",
                        },
                    },
                }
            ],
        )
    create_hub_event.assert_awaited_once()

    assert "Created scheduled action" in created[0].result
    row = integration_env.db.execute(
        "SELECT id FROM actions WHERE intent = ? ORDER BY created_at DESC LIMIT 1",
        ("Schedule callback",),
    ).fetchone()
    assert row is not None
    action_id = str(row["id"])

    stored = get_action_by_id(integration_env.db, action_id)
    assert stored is not None
    assert stored.hub_sync_status == "pending"

    integration_env.db.execute(
        "UPDATE actions SET hub_event_id = 'evt-123', hub_sync_status = 'synced' WHERE id = ?",
        (action_id,),
    )
    integration_env.db.commit()

    with (
        patch.object(manage_appointment_module, "send_sms", new=AsyncMock(return_value="SM456")),
        patch.object(manage_appointment_module, "update_hub_event", new=AsyncMock(return_value=True)) as update_hub_event,
        patch.object(manage_appointment_module, "delete_hub_event", new=AsyncMock(return_value=True)) as delete_hub_event,
    ):
        managed = await execute_tool_calls(
            integration_env.db,
            SkillContext(
                audience="public",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id=call.id,
                person_id=person.id,
                source="mid-call",
            ),
            [
                {
                    "id": "reschedule",
                    "function": {
                        "name": "manage-appointment",
                        "arguments": {
                            "id": action_id,
                            "operation": "reschedule",
                            "start_at": "2026-04-01T15:00:00Z",
                            "end_at": "2026-04-01T15:30:00Z",
                        },
                    },
                },
                {
                    "id": "cancel",
                    "function": {
                        "name": "manage-appointment",
                        "arguments": {
                            "id": action_id,
                            "operation": "cancel",
                        },
                    },
                },
            ],
        )
    update_hub_event.assert_awaited_once()
    delete_hub_event.assert_awaited_once()

    assert "Rescheduled appointment" in managed[0].result
    assert "Cancelled appointment" in managed[1].result
    stored = get_action_by_id(integration_env.db, action_id)
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.hub_sync_status == "pending"
