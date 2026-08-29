from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import mystic.calendar as calendar_module
from mystic.config import OAuthTokens, clear_config_cache, save_hub_tokens
from mystic.db import (
    close_database,
    get_action_by_id,
    initialize_schema,
    insert_action,
    open_database,
    upsert_external_event,
    upsert_person,
)
from mystic.http import HttpResponse
from tests.python_helpers import TEST_PROVIDERS_CONFIG, TempAppHome, seed_core_files


ICAL_DEPS_AVAILABLE = bool(importlib.util.find_spec("icalendar")) and bool(
    importlib.util.find_spec("recurring_ical_events")
)


class CalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["calendar"] = {
            "subscriptions": [{"url": "https://example.test/work.ics", "label": "Work"}],
            "syncIntervalMinutes": 15,
            "reminderMinutes": 10,
        }
        seed_core_files(self.home, providers=providers)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        calendar_module._last_sync_at = 0
        calendar_module._last_refresh_at = 0
        calendar_module._notified_ids = set()
        self.person = upsert_person(self.db, "+15550001111", "Alice")

    def tearDown(self) -> None:
        clear_config_cache()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def _configure_hub(self, payload: dict[str, object]) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["calendar"] = {
            "subscriptions": [{"url": "https://example.test/work.ics", "label": "Work"}],
            "syncIntervalMinutes": 15,
            "reminderMinutes": 10,
            "hub": payload,
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache()

    @unittest.skipUnless(ICAL_DEPS_AVAILABLE, "calendar dependencies not installed in local env")
    def test_expand_events_handles_all_day_default_duration(self) -> None:
        raw_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:evt-1\r\n"
            "DTSTART;VALUE=DATE:20260401\r\n"
            "SUMMARY:All Day Event\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        calendar = calendar_module.parse_ics_feed(raw_ics)
        events = calendar_module.expand_events(
            calendar,
            datetime(2026, 3, 31, tzinfo=UTC),
            datetime(2026, 4, 3, tzinfo=UTC),
        )

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["all_day"])
        self.assertEqual(events[0]["end_at"] - events[0]["start_at"], 24 * 60 * 60 * 1000)

    def test_format_event_time_uses_timezone(self) -> None:
        timestamp_ms = int(datetime(2026, 4, 1, 14, 0, tzinfo=UTC).timestamp() * 1000)
        self.assertEqual(
            calendar_module.format_event_time(timestamp_ms, ZoneInfo("America/New_York")),
            "10:00 AM",
        )

    async def test_sync_subscription_upserts_and_deletes_stale_events(self) -> None:
        upsert_external_event(
            self.db,
            ics_uid="stale",
            ics_url="https://example.test/work.ics",
            title="Old event",
            start_at=1,
            end_at=2,
        )

        with (
            patch(
                "mystic.calendar.fetch_with_timeout",
                new=AsyncMock(return_value=HttpResponse(status_code=200, content=b"BEGIN:VCALENDAR"))),
            patch("mystic.calendar.parse_ics_feed", return_value=object()),
            patch(
                "mystic.calendar.expand_events",
                return_value=[
                    {
                        "uid": "evt-1",
                        "title": "Calendar event",
                        "description": "Details",
                        "location": "Room A",
                        "start_at": 1_775_052_000_000,
                        "end_at": 1_775_053_800_000,
                        "all_day": False,
                    }
                ],
            ),
        ):
            count = await calendar_module.sync_subscription(
                self.db,
                calendar_module.CalendarSubscription(url="https://example.test/work.ics", label="Work"),
            )

        self.assertEqual(count, 1)
        rows = self.db.execute("SELECT title, ics_uid FROM external_events ORDER BY title ASC").fetchall()
        self.assertEqual([(row["title"], row["ics_uid"]) for row in rows], [("Calendar event", "evt-1")])

    async def test_maybe_sync_honors_interval(self) -> None:
        with (
            patch("mystic.calendar.now_ms", side_effect=[1_000_000, 1_005_000, 1_905_000]),
            patch("mystic.calendar.sync_subscription", new=AsyncMock()) as sync_subscription,
        ):
            await calendar_module.maybe_sync(self.db)
            await calendar_module.maybe_sync(self.db)
            await calendar_module.maybe_sync(self.db)

        self.assertEqual(sync_subscription.await_count, 2)

    def test_availability_and_open_slots_merge_events_and_actions(self) -> None:
        upsert_external_event(
            self.db,
            ics_uid="evt-1",
            ics_url="https://example.test/work.ics",
            title="Busy block",
            start_at=100,
            end_at=200,
        )
        insert_action(
            self.db,
            person_id=self.person.id,
            intent="Scheduled callback",
            source="owner",
            start_at=250,
            end_at=350,
        )

        available, conflicts = calendar_module.check_availability(self.db, 150, 175)
        self.assertFalse(available)
        self.assertEqual(conflicts, ["Busy block"])

        slots = calendar_module.find_open_slots(self.db, 0, 400, min_duration_ms=40)
        self.assertEqual(slots, [(0, 100), (200, 250), (350, 400)])

    async def test_check_reminders_deduplicates_notifications(self) -> None:
        upsert_external_event(
            self.db,
            ics_uid="evt-1",
            ics_url="https://example.test/work.ics",
            title="Upcoming meeting",
            start_at=1_775_055_300_000,
            end_at=1_775_057_100_000,
        )
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Scheduled callback",
            source="owner",
            start_at=1_775_055_450_000,
            end_at=1_775_057_250_000,
        )

        with (
            patch("mystic.db.now_ms", return_value=1_775_055_000_000),
            patch("mystic.actions.notify", new=AsyncMock(return_value=True)) as notify,
        ):
            await calendar_module.check_reminders(self.db)
            await calendar_module.check_reminders(self.db)

        self.assertEqual(notify.await_count, 2)
        self.assertIn(action.id, calendar_module._notified_ids)

    async def test_create_update_and_delete_hub_event_google(self) -> None:
        self._configure_hub(
            {
                "provider": "google",
                "calendarId": "primary",
                "clientId": "client-id",
                "clientSecret": "client-secret",
                "writeEnabled": True,
            }
        )
        save_hub_tokens(
            OAuthTokens(
                access_token="token-1",
                refresh_token="refresh-1",
                expires_at=4_000_000_000,
            )
        )
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Hub sync meeting",
            source="owner",
            start_at=1_775_055_450_000,
            end_at=1_775_057_250_000,
            hub_sync_status="pending",
        )

        with patch(
            "mystic.calendar.fetch_with_timeout",
            new=AsyncMock(
                side_effect=[
                    HttpResponse(status_code=200, content=b'{"id":"evt-123"}'),
                    HttpResponse(status_code=200, content=b"{}"),
                    HttpResponse(status_code=204, content=b""),
                ]
            ),
        ):
            created = await calendar_module.create_hub_event(self.db, action)
            stored_after_create = get_action_by_id(self.db, action.id)
            assert stored_after_create is not None
            updated = await calendar_module.update_hub_event(self.db, stored_after_create)
            stored_after_update = get_action_by_id(self.db, action.id)
            assert stored_after_update is not None
            deleted = await calendar_module.delete_hub_event(self.db, stored_after_update)

        self.assertTrue(created)
        self.assertTrue(updated)
        self.assertTrue(deleted)
        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertIsNone(stored.hub_event_id)
        self.assertIsNone(stored.hub_sync_status)

    async def test_create_hub_event_retries_once_after_401(self) -> None:
        self._configure_hub(
            {
                "provider": "google",
                "calendarId": "primary",
                "clientId": "client-id",
                "clientSecret": "client-secret",
                "writeEnabled": True,
            }
        )
        save_hub_tokens(
            OAuthTokens(
                access_token="expired-token",
                refresh_token="refresh-1",
                expires_at=4_000_000_000,
            )
        )
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Retry after 401",
            source="owner",
            start_at=1_775_055_450_000,
            end_at=1_775_057_250_000,
            hub_sync_status="pending",
        )

        async def fake_fetch(url: str, **kwargs: Any) -> HttpResponse:
            if "oauth2.googleapis.com/token" in url:
                return HttpResponse(
                    status_code=200,
                    content=b'{"access_token":"fresh-token","refresh_token":"refresh-2","expires_in":3600}',
                )
            if kwargs.get("method") == "POST":
                headers = kwargs.get("headers")
                auth = headers.get("Authorization") if isinstance(headers, dict) else None
                if auth == "Bearer expired-token":
                    return HttpResponse(status_code=401, content=b"{}")
                return HttpResponse(status_code=200, content=b'{"id":"evt-401"}')
            raise AssertionError(f"Unexpected request: {url}")

        with patch("mystic.calendar.fetch_with_timeout", new=AsyncMock(side_effect=fake_fetch)):
            self.assertTrue(await calendar_module.create_hub_event(self.db, action))

        stored = get_action_by_id(self.db, action.id)
        assert stored is not None
        self.assertEqual(stored.hub_event_id, "evt-401")
        self.assertEqual(stored.hub_sync_status, "synced")

    async def test_maybe_retry_hub_sync_dispatches_by_action_state(self) -> None:
        self._configure_hub(
            {
                "provider": "caldav",
                "calendarId": "/dav/calendars/test/default/",
                "baseUrl": "https://nextcloud.example.test",
                "username": "alice",
                "password": "secret",
                "writeEnabled": True,
            }
        )
        create_action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Create me",
            source="owner",
            start_at=100,
            end_at=200,
            hub_sync_status="pending",
        )
        update_action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Update me",
            source="owner",
            start_at=300,
            end_at=400,
            hub_sync_status="pending",
        )
        self.db.execute(
            "UPDATE actions SET hub_event_id = 'evt-update', hub_sync_status = 'pending' WHERE id = ?",
            (update_action.id,),
        )
        cancel_action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Cancel me",
            source="owner",
            start_at=500,
            end_at=600,
            hub_sync_status="pending",
        )
        self.db.execute(
            "UPDATE actions SET status = 'cancelled', hub_event_id = 'evt-delete' WHERE id = ?",
            (cancel_action.id,),
        )
        failed_action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Fail me",
            source="owner",
            start_at=700,
            end_at=800,
            hub_sync_status="pending",
        )
        self.db.commit()

        with (
            patch("mystic.calendar.create_hub_event", new=AsyncMock(return_value=True)) as create_hub_event,
            patch("mystic.calendar.update_hub_event", new=AsyncMock(return_value=True)) as update_hub_event,
            patch("mystic.calendar.delete_hub_event", new=AsyncMock(return_value=True)) as delete_hub_event,
        ):
            await calendar_module.maybe_retry_hub_sync(self.db)

        create_hub_event.assert_any_await(self.db, create_action)
        create_hub_event.assert_any_await(self.db, failed_action)
        update_hub_event.assert_awaited_once()
        delete_hub_event.assert_awaited_once()
        assert update_hub_event.await_args is not None
        assert delete_hub_event.await_args is not None
        self.assertEqual(update_hub_event.await_args.args[0], self.db)
        self.assertEqual(update_hub_event.await_args.args[1].id, update_action.id)
        self.assertEqual(delete_hub_event.await_args.args[0], self.db)
        self.assertEqual(delete_hub_event.await_args.args[1].id, cancel_action.id)

    async def test_maybe_retry_hub_sync_marks_failed_after_retry_limit(self) -> None:
        self._configure_hub(
            {
                "provider": "caldav",
                "calendarId": "/dav/calendars/test/default/",
                "baseUrl": "https://nextcloud.example.test",
                "username": "alice",
                "password": "secret",
                "writeEnabled": True,
            }
        )
        failed_action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Fail me",
            source="owner",
            start_at=700,
            end_at=800,
            hub_sync_status="pending",
        )

        with (
            patch("mystic.calendar.create_hub_event", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("mystic.calendar.update_hub_event", new=AsyncMock(return_value=True)),
            patch("mystic.calendar.delete_hub_event", new=AsyncMock(return_value=True)),
        ):
            await calendar_module.maybe_retry_hub_sync(self.db)
            await calendar_module.maybe_retry_hub_sync(self.db)
            await calendar_module.maybe_retry_hub_sync(self.db)

        failed = get_action_by_id(self.db, failed_action.id)
        assert failed is not None
        self.assertEqual(failed.hub_sync_status, "failed")
        self.assertEqual(failed.hub_sync_attempts, 3)
