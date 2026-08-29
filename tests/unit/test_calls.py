from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from mystic.actions import start_action_attempt
from mystic.calls import (
    _pending_bridge_tasks,
    add_active_call,
    drain_pending_bridge_tasks,
    drain_pending_extraction_tasks,
    get_active_call_count,
    handle_completed_call_status,
    handle_end_of_call_report,
    handle_incoming_call,
    handle_unanswered_outbound,
    initiate_bootstrap_call,
    initiate_outbound_call,
    reconnect_call_to_stream,
    reset_active_calls,
    resume_call,
    set_call_ended_callback,
    set_extraction_pipeline,
    set_transfer_target_sid,
)
from mystic.config import get_providers_config
from mystic.types import CallState
from mystic.db import get_action_by_id, insert_action, get_call_by_external_id, insert_call, close_database, initialize_schema, open_database, get_person_by_phone, upsert_person
from tests.python_helpers import TempAppHome, seed_core_files

OWNER_PHONE = "+15551234567"
PUBLIC_PHONE = "+15550001111"
TUNNEL_URL = "https://example.tail1234.ts.net"
SAMPLE_TRANSCRIPT = "Caller asked for a Tuesday follow-up."


class CallFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_active_calls(self.db)
        set_extraction_pipeline(None)
        set_call_ended_callback(None)

    def tearDown(self) -> None:
        set_extraction_pipeline(None)
        set_call_ended_callback(None)
        reset_active_calls(self.db)
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_handle_incoming_call_creates_person_call_and_twiml(self) -> None:
        with patch(
            "mystic.calls.create_room",
            new=AsyncMock(return_value="call-room-001"),
        ):
            result = await handle_incoming_call(self.db, PUBLIC_PHONE, "CA-in-001", TUNNEL_URL)

        self.assertIn("twiml", result)
        twiml = result["twiml"]  # type: ignore[index]
        self.assertIn("<Stream", twiml)
        self.assertIn("media-stream", twiml)
        self.assertIn("token=", twiml)
        self.assertIn("expiresAt=", twiml)

        person = get_person_by_phone(self.db, PUBLIC_PHONE)
        call = get_call_by_external_id(self.db, "CA-in-001")
        self.assertIsNotNone(person)
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.direction, "inbound")
        self.assertEqual(call.audience, "public")
        self.assertEqual(get_active_call_count(self.db), 1)

    async def test_initiate_outbound_call_marks_action_in_progress(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Bob")
        action = insert_action(self.db, person_id=person.id, intent="Follow up", source="agent")

        with (
            patch(
                "mystic.calls.create_room",
                new=AsyncMock(return_value="call-room-002"),
            ),
            patch(
                "mystic.calls.make_outbound_call",
                new=AsyncMock(return_value="CA-out-001"),
            ),
        ):
            call_id = await initiate_outbound_call(self.db, action, TUNNEL_URL)

        self.assertIsNotNone(call_id)
        call = get_call_by_external_id(self.db, "CA-out-001")
        updated_action = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(call)
        self.assertIsNotNone(updated_action)
        assert call is not None
        assert updated_action is not None
        self.assertEqual(call.action_id, action.id)
        self.assertEqual(call.direction, "outbound")
        self.assertEqual(updated_action.status, "in_progress")
        self.assertEqual(updated_action.attempts, 1)
        self.assertEqual(get_active_call_count(self.db), 1)

    async def test_initiate_outbound_call_cleans_up_on_twilio_failure(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Bob")
        action = insert_action(self.db, person_id=person.id, intent="Follow up", source="agent")

        with (
            patch(
                "mystic.calls.create_room",
                new=AsyncMock(return_value="call-room-003"),
            ),
            patch(
                "mystic.calls.delete_room",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "mystic.calls.make_outbound_call",
                new=AsyncMock(side_effect=RuntimeError("twilio down")),
            ),
        ):
            call_id = await initiate_outbound_call(self.db, action, TUNNEL_URL)

        self.assertIsNone(call_id)
        action_row = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(action_row)
        assert action_row is not None
        self.assertEqual(action_row.status, "pending")
        self.assertEqual(action_row.attempts, 0)

        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM calls WHERE action_id = ?",
            (action.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["count"]), 0)

    async def test_handle_end_of_call_report_updates_call_and_invokes_pipeline(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-end-001",
        )
        seen: list[tuple[str, str, bool]] = []
        extraction = AsyncMock(return_value=None)
        set_extraction_pipeline(extraction)
        set_call_ended_callback(lambda call_id, external_id, answered: seen.append((call_id, external_id, answered)))

        await handle_end_of_call_report(self.db, "CA-end-001", SAMPLE_TRANSCRIPT, 75)
        await asyncio.sleep(0)

        updated = get_call_by_external_id(self.db, "CA-end-001")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.transcript, SAMPLE_TRANSCRIPT)
        self.assertEqual(updated.duration, 75)
        self.assertIsNotNone(updated.ended_at)
        extraction.assert_awaited_once_with(self.db, call.id, person.id, SAMPLE_TRANSCRIPT)
        self.assertEqual(seen, [(call.id, "CA-end-001", True)])

    async def test_reconnect_call_to_stream_builds_stream_twiml_for_existing_call(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="public",
            external_id="CA-reconnect-001",
        )

        with (
            patch("mystic.calls.assemble_context", return_value="system prompt"),
            patch("mystic.calls.create_room", new=AsyncMock(return_value="call-room-reconnect")),
            patch(
                "mystic.calls.build_authenticated_media_stream_url",
                return_value="wss://example.test/media-stream",
            ),
        ):
            twiml = await reconnect_call_to_stream(self.db, call.id, TUNNEL_URL)

        self.assertEqual(
            twiml,
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Connect><Stream url="wss://example.test/media-stream">'
            f'<Parameter name="callId" value="{call.id}" /></Stream></Connect></Response>',
        )

    async def test_reconnect_call_to_stream_returns_none_for_unknown_call(self) -> None:
        self.assertIsNone(await reconnect_call_to_stream(self.db, "missing-call-id", TUNNEL_URL))

    async def test_reconnect_call_to_stream_returns_none_when_person_missing(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="public",
            external_id="CA-reconnect-missing-person",
        )

        with patch("mystic.calls.get_person_by_id", return_value=None):
            twiml = await reconnect_call_to_stream(self.db, call.id, TUNNEL_URL)

        self.assertIsNone(twiml)

    async def test_resume_call_rebuilds_stream_and_updates_live_call(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="public",
            external_id="CA-resume-001",
        )
        providers = get_providers_config()
        assert providers.twilio is not None

        with (
            patch("mystic.calls.get_tunnel_url", return_value=TUNNEL_URL),
            patch("mystic.calls.reconnect_call_to_stream", new=AsyncMock(return_value="<Response />")),
            patch("mystic.calls.update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            resumed = await resume_call(self.db, call.id)

        self.assertTrue(resumed)
        update_live_call_mock.assert_awaited_once_with(
            providers.twilio,
            "CA-resume-001",
            twiml="<Response />",
        )

    async def test_resume_call_returns_false_for_unknown_call(self) -> None:
        self.assertFalse(await resume_call(self.db, "missing-call-id"))

    async def test_handle_completed_call_status_ends_active_transfer_target(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="inbound",
            audience="public",
            external_id="CA-completed-transfer-001",
        )
        providers = get_providers_config()
        assert providers.twilio is not None
        set_transfer_target_sid(call.id, "CA-target-001")

        with patch("mystic.calls.end_call", new=AsyncMock()) as end_call_mock:
            await handle_completed_call_status(self.db, "CA-completed-transfer-001", 12)

        end_call_mock.assert_awaited_once_with(providers.twilio, "CA-target-001")

    async def test_drain_pending_extraction_tasks_cancels_queued_work(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-end-cancel-001",
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def extraction(*_args: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        set_extraction_pipeline(extraction)

        await handle_end_of_call_report(self.db, "CA-end-cancel-001", SAMPLE_TRANSCRIPT, 15)
        await asyncio.wait_for(started.wait(), timeout=1)
        await drain_pending_extraction_tasks(0)
        await asyncio.wait_for(cancelled.wait(), timeout=1)

    def test_handle_unanswered_outbound_requeues_action(self) -> None:
        person = upsert_person(self.db, PUBLIC_PHONE, "Alice")
        action = insert_action(self.db, person_id=person.id, intent="Call back", source="agent")
        start_action_attempt(self.db, action.id)
        call = insert_call(
            self.db,
            person_id=person.id,
            direction="outbound",
            audience="public",
            external_id="CA-miss-001",
            action_id=action.id,
        )
        add_active_call(
            CallState(
                call_id=call.id,
                person_id=person.id,
                person_name=person.name,
                audience="public",
                direction="outbound",
                channel="phone",
                modality="voice",
                started_at=call.started_at,
            ),
            self.db,
        )

        handle_unanswered_outbound(self.db, "CA-miss-001")

        updated_call = get_call_by_external_id(self.db, "CA-miss-001")
        updated_action = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(updated_call)
        self.assertIsNotNone(updated_action)
        assert updated_call is not None
        assert updated_action is not None
        self.assertIsNotNone(updated_call.ended_at)
        self.assertEqual(updated_action.status, "pending")
        self.assertEqual(updated_action.result, "Call was not answered.")
        self.assertEqual(get_active_call_count(self.db), 0)

    async def test_initiate_bootstrap_call_marks_owner_action_in_progress(self) -> None:
        owner = upsert_person(self.db, OWNER_PHONE, "Owner")
        action = insert_action(
            self.db,
            person_id=owner.id,
            intent="Get to know owner",
            context="Bootstrap: discover identity.",
            source="cli",
        )
        providers = get_providers_config()
        assert providers.twilio is not None
        captured_metadata: dict[str, object] = {}

        async def capture_room(_lk: object, _call_id: str, metadata: dict[str, object]) -> str:
            captured_metadata.update(metadata)
            return "call-room-bootstrap"

        with (
            patch(
                "mystic.calls.create_room",
                new=AsyncMock(side_effect=capture_room),
            ),
            patch(
                "mystic.calls.make_outbound_call",
                new=AsyncMock(return_value="CA-bootstrap-001"),
            ),
        ):
            result = await initiate_bootstrap_call(
                db=self.db,
                twilio_config=providers.twilio,
                livekit_config=providers.livekit,
                customer_phone=OWNER_PHONE,
                person_id=owner.id,
                action_id=action.id,
                voice_id="Mark",
                tunnel_url=TUNNEL_URL,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["call_sid"], "CA-bootstrap-001")
        call = get_call_by_external_id(self.db, "CA-bootstrap-001")
        updated_action = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(call)
        self.assertIsNotNone(updated_action)
        assert call is not None
        assert updated_action is not None
        self.assertEqual(call.audience, "owner")
        self.assertEqual(updated_action.status, "in_progress")
        self.assertEqual(updated_action.attempts, 1)
        self.assertTrue(captured_metadata["bootstrap"])
        self.assertNotIn("firstMessage", captured_metadata)


class LocalEscalationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_active_calls(self.db)
        set_extraction_pipeline(None)
        set_call_ended_callback(None)

    def tearDown(self) -> None:
        set_extraction_pipeline(None)
        set_call_ended_callback(None)
        reset_active_calls(self.db)
        # Clean up any bridge tasks
        for task in list(_pending_bridge_tasks):
            if not task.done():
                task.cancel()
        _pending_bridge_tasks.clear()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_drain_pending_bridge_tasks_cancels_queued_work(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fake_bridge_work() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(fake_bridge_work())
        _pending_bridge_tasks.add(task)
        task.add_done_callback(_pending_bridge_tasks.discard)

        await asyncio.wait_for(started.wait(), timeout=1)
        await drain_pending_bridge_tasks(0)
        await asyncio.wait_for(cancelled.wait(), timeout=1)


if __name__ == "__main__":
    unittest.main()
