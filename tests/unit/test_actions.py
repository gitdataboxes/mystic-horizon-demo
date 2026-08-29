from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from mystic.actions import (
    DEFAULT_ACTION_RETRY_DELAY_MS,
    append_action_context,
    apply_decision,
    attempt_action,
    cancel_action,
    check_satisfaction,
    complete_action,
    drain_scheduler,
    escalate_to_owner,
    finalize_in_progress_action,
    notify,
    reschedule_action,
    scheduler_tick,
    send_email,
    start_action_attempt,
    start_scheduler,
    stop_scheduler,
)
from mystic.config import SmtpConfig
from mystic.types import Action, SchedulerJudgment
from mystic.db import get_action_by_id, get_all_pending_actions, insert_action, insert_call, update_call_end, update_call_summary, close_database, initialize_schema, open_database, get_person_by_phone, upsert_person
from mystic.skills import init_skills, reset_registry
from tests.python_helpers import TempAppHome, seed_core_files


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="agent@example.com",
    )


class ActionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        self.person = upsert_person(self.db, "+15550001111", "Alice")

    def tearDown(self) -> None:
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def test_complete_cancel_and_reschedule_action(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Call Alice back", source="agent")
        reschedule_action(self.db, action.id, 1_700_000_000_000)
        cancel_action(self.db, action.id, "No longer needed")
        complete_action(self.db, action.id, "Finished")

        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.due_at, 1_700_000_000_000)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result, "Finished")

    def test_attempt_action_auto_fails_at_max_attempts(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Retry task", source="agent")
        self.assertFalse(attempt_action(self.db, action.id))
        self.assertFalse(attempt_action(self.db, action.id))
        self.assertTrue(attempt_action(self.db, action.id))

        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.attempts, stored.max_attempts)
        self.assertEqual(stored.result, f"Max attempts reached ({stored.max_attempts})")

    def test_finalize_in_progress_action_requeues_or_fails(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Reach Alice", source="agent")
        start_action_attempt(self.db, action.id)
        finalize_in_progress_action(self.db, action.id, "No answer.")

        requeued = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(requeued)
        assert requeued is not None
        self.assertEqual(requeued.status, "pending")
        self.assertEqual(requeued.result, "No answer.")
        self.assertIsNotNone(requeued.due_at)
        assert requeued.due_at is not None
        self.assertGreaterEqual(requeued.due_at, requeued.updated_at)
        self.assertLessEqual(requeued.due_at - requeued.updated_at, DEFAULT_ACTION_RETRY_DELAY_MS)

        exhausted = insert_action(self.db, person_id=self.person.id, intent="Escalate", source="agent")
        start_action_attempt(self.db, exhausted.id)
        with self.db:
            self.db.execute(
                "UPDATE actions SET attempts = max_attempts WHERE id = ?",
                (exhausted.id,),
            )
        finalize_in_progress_action(self.db, exhausted.id, "Still unreachable.")

        failed = get_action_by_id(self.db, exhausted.id)
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.status, "failed")
        self.assertIn("Still unreachable.", failed.result or "")
        self.assertIn("Max attempts reached", failed.result or "")

    def test_append_action_context_adds_timestamped_note(self) -> None:
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Send summary",
            source="agent",
            context="Initial context",
        )
        append_action_context(self.db, action.id, "Mentioned in follow-up call")

        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIn("Initial context", stored.context or "")
        self.assertIn("Mentioned in follow-up call", stored.context or "")


class SatisfactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        init_skills()
        self.person = upsert_person(self.db, "+15550002222", "Dave")

    def tearDown(self) -> None:
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_check_satisfaction_completes_partial_and_ignores(self) -> None:
        satisfied = insert_action(self.db, person_id=self.person.id, intent="Schedule meeting", source="post-call")
        partial = insert_action(self.db, person_id=self.person.id, intent="Send report", source="post-call")
        untouched = insert_action(self.db, person_id=self.person.id, intent="Review contract", source="post-call")
        call = insert_call(self.db, person_id=self.person.id, direction="inbound", audience="public")
        update_call_end(self.db, call.id, transcript="We scheduled the meeting and discussed the report.")
        update_call_summary(self.db, call.id, "Meeting scheduled; report discussed but not sent.")

        raw = (
            "["
            f'{{"id":"{satisfied.id}","status":"satisfied","confidence":0.95,"reason":"Meeting was scheduled."}},'
            f'{{"id":"{partial.id}","status":"partial","confidence":0.7,"reason":"Report discussed but not sent."}},'
            f'{{"id":"{untouched.id}","status":"not_satisfied","confidence":0.9,"reason":"Contract not discussed."}},'
            '{"id":"ghost","status":"satisfied","confidence":0.1,"reason":"Ignore me."}'
            "]"
        )

        with patch("mystic.skills.execute_cognitive_skill", new=AsyncMock(return_value=raw)):
            await check_satisfaction(self.db, call.id, self.person.id)

        satisfied_row = get_action_by_id(self.db, satisfied.id)
        partial_row = get_action_by_id(self.db, partial.id)
        untouched_row = get_action_by_id(self.db, untouched.id)
        self.assertEqual(satisfied_row.status if satisfied_row else None, "completed")
        self.assertEqual(partial_row.status if partial_row else None, "pending")
        assert partial_row is not None
        self.assertIn("Partially addressed", partial_row.context or "")
        self.assertEqual(untouched_row.status if untouched_row else None, "pending")
        self.assertIsNone(untouched_row.context if untouched_row else None)

    async def test_check_satisfaction_exits_without_open_actions(self) -> None:
        call = insert_call(self.db, person_id=self.person.id, direction="inbound", audience="public")
        with patch("mystic.skills.execute_cognitive_skill", new=AsyncMock()) as execute:
            await check_satisfaction(self.db, call.id, self.person.id)
        execute.assert_not_called()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        init_skills()
        self.person = upsert_person(self.db, "+15550003333", "Eve")

    async def asyncTearDown(self) -> None:
        await drain_scheduler(1000)

    def tearDown(self) -> None:
        stop_scheduler()
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_apply_decision_act_retries_when_call_start_fails(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Call Eve", source="agent")
        judgment = SchedulerJudgment(id=action.id, decision="act", reason="Due now")

        async def no_call(*_args: object, **_kwargs: object) -> str | None:
            return None

        await apply_decision(
            self.db,
            action,
            judgment,
            "https://example.test",
            initiate_outbound_call=no_call,
        )

        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.attempts, 1)
        self.assertIsNotNone(stored.due_at)

    async def test_apply_decision_act_routes_bootstrap_to_twilio(self) -> None:
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Get to know owner",
            context="Bootstrap conversation to establish identity and soul.",
            source="cli",
        )
        judgment = SchedulerJudgment(id=action.id, decision="act", reason="Start bootstrap")
        generic_call = AsyncMock()

        with patch(
            "mystic.calls.initiate_bootstrap_call",
            new=AsyncMock(return_value={"call_id": "call-bootstrap-001"}),
        ) as bootstrap_call:
            await apply_decision(
                self.db,
                action,
                judgment,
                "https://example.test",
                initiate_outbound_call=generic_call,
            )

        bootstrap_call.assert_awaited_once_with(
            db=self.db,
            twilio_config=ANY,
            livekit_config=ANY,
            customer_phone="+15551234567",
            person_id=self.person.id,
            action_id=action.id,
            voice_id="Hades",
            tunnel_url="https://example.test",
        )
        generic_call.assert_not_awaited()

    async def test_apply_decision_wait_uses_llm_timestamp(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Call later", source="agent")
        judgment = SchedulerJudgment(
            id=action.id,
            decision="wait",
            reason="Outside business hours",
            wait_until="2026-03-12T15:30:00Z",
        )

        await apply_decision(self.db, action, judgment, "https://example.test")
        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.due_at, 1_773_329_400_000)

    async def test_apply_notify_decision(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Review notes", source="agent")
        judgment = SchedulerJudgment(
            id=action.id,
            decision="notify",
            reason="Low urgency reminder for the owner",
        )

        with patch("mystic.actions.notify", new=AsyncMock(return_value=True)) as mock_notify:
            await apply_decision(self.db, action, judgment, "https://example.test")

        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.attempts, 0)
        self.assertIsNotNone(stored.due_at)
        assert stored.due_at is not None
        self.assertGreaterEqual(stored.due_at, stored.updated_at)
        self.assertLessEqual(stored.due_at - stored.updated_at, DEFAULT_ACTION_RETRY_DELAY_MS)
        mock_notify.assert_awaited_once_with(
            "Action due: Review notes",
            "Low urgency reminder for the owner",
        )

    async def test_apply_decision_escalate_creates_owner_action_and_cancels_original(self) -> None:
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Need approval",
            context="Customer is waiting",
            source="agent",
        )
        started_actions: list[str] = []

        async def fake_call(_db: object, escalation: Action, _url: str) -> str | None:
            started_actions.append(escalation.id)
            return "call-123"

        escalation_action_id = await escalate_to_owner(
            self.db,
            action,
            "https://example.test",
            "Owner should handle this personally",
            initiate_outbound_call=fake_call,
        )
        cancel_judgment = SchedulerJudgment(
            id=action.id,
            decision="escalate",
            reason="Owner should handle this personally",
        )
        await apply_decision(
            self.db,
            action,
            cancel_judgment,
            "https://example.test",
            initiate_outbound_call=fake_call,
        )

        original = get_action_by_id(self.db, action.id)
        owner = get_person_by_phone(self.db, "+15551234567")
        self.assertIsNotNone(owner)
        assert owner is not None
        self.assertIn(escalation_action_id, started_actions)
        self.assertEqual(original.status if original else None, "cancelled")
        assert original is not None
        self.assertIn("Escalated to owner via action", original.result or "")
        pending_owner_actions = [item for item in get_all_pending_actions(self.db) if item.person_id == owner.id]
        self.assertGreaterEqual(len(pending_owner_actions), 1)
        self.assertTrue(any(item.intent.startswith("Escalation: Need approval") for item in pending_owner_actions))

    async def test_scheduler_tick_auto_cancels_bootstrap_and_applies_judgment(self) -> None:
        bootstrap = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Get to know owner",
            source="agent",
        )
        normal = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Call Eve now",
            source="agent",
        )
        with (
            patch(
                "mystic.skills.execute_cognitive_skill",
                new=AsyncMock(
                    return_value=(
                        "["
                        f'{{"id":"{normal.id}","decision":"cancel","reason":"No longer relevant"}}'
                        "]"
                    )
                ),
            ),
            patch("mystic.calendar.maybe_sync", new=AsyncMock()) as maybe_sync,
            patch("mystic.calendar.check_reminders", new=AsyncMock()) as check_reminders,
            patch("mystic.calendar.maybe_retry_hub_sync", new=AsyncMock()) as maybe_retry_hub_sync,
        ):
            await scheduler_tick(self.db, "https://example.test")

        bootstrap_row = get_action_by_id(self.db, bootstrap.id)
        normal_row = get_action_by_id(self.db, normal.id)
        self.assertEqual(bootstrap_row.status if bootstrap_row else None, "cancelled")
        self.assertEqual(normal_row.status if normal_row else None, "cancelled")
        self.assertEqual(normal_row.result if normal_row else None, "No longer relevant")
        maybe_sync.assert_awaited_once_with(self.db)
        check_reminders.assert_awaited_once_with(self.db)
        maybe_retry_hub_sync.assert_awaited_once_with(self.db)

    async def test_scheduler_tick_reschedules_local_bootstrap_without_judgment(self) -> None:
        bootstrap = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Get to know owner",
            context="Bootstrap conversation to establish identity and soul.",
            source="cli",
        )
        with (
            patch("mystic.actions.identity_exists", return_value=False),
            patch("mystic.actions.soul_exists", return_value=False),
            patch("mystic.actions.get_providers_config", return_value=SimpleNamespace(twilio=None)),
            patch("mystic.skills.execute_cognitive_skill", new=AsyncMock()) as execute_cognitive_skill,
            patch("mystic.calendar.maybe_sync", new=AsyncMock()) as maybe_sync,
            patch("mystic.calendar.check_reminders", new=AsyncMock()) as check_reminders,
            patch("mystic.calendar.maybe_retry_hub_sync", new=AsyncMock()) as maybe_retry_hub_sync,
        ):
            await scheduler_tick(self.db, "https://example.test")

        bootstrap_row = get_action_by_id(self.db, bootstrap.id)
        self.assertIsNotNone(bootstrap_row)
        assert bootstrap_row is not None
        self.assertEqual(bootstrap_row.status, "pending")
        self.assertEqual(bootstrap_row.attempts, 0)
        self.assertIsNotNone(bootstrap_row.due_at)
        assert bootstrap_row.due_at is not None
        self.assertGreaterEqual(bootstrap_row.due_at, bootstrap_row.updated_at)
        self.assertLessEqual(bootstrap_row.due_at - bootstrap_row.updated_at, DEFAULT_ACTION_RETRY_DELAY_MS)
        execute_cognitive_skill.assert_not_awaited()
        maybe_sync.assert_awaited_once_with(self.db)
        check_reminders.assert_awaited_once_with(self.db)
        maybe_retry_hub_sync.assert_awaited_once_with(self.db)

    async def test_start_scheduler_runs_immediately_and_stop_cancels(self) -> None:
        action = insert_action(self.db, person_id=self.person.id, intent="Call Eve", source="agent")
        with patch(
            "mystic.skills.execute_cognitive_skill",
            new=AsyncMock(
                return_value=(
                    "["
                    f'{{"id":"{action.id}","decision":"cancel","reason":"Already handled"}}'
                    "]"
                )
            ),
        ):
            start_scheduler(self.db, "https://example.test", interval_ms=10)
            await asyncio.sleep(0.05)
            await drain_scheduler(1000)

        stored = get_action_by_id(self.db, action.id)
        self.assertEqual(stored.status if stored else None, "cancelled")
        self.assertEqual(stored.result if stored else None, "Already handled")

    async def test_drain_scheduler_waits_for_inflight_tick_cancellation(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_tick(*_args: object, **_kwargs: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch(
            "mystic.actions.scheduler_tick",
            new=AsyncMock(side_effect=blocking_tick),
        ):
            start_scheduler(self.db, "https://example.test", interval_ms=10)
            await asyncio.wait_for(started.wait(), timeout=1)
            await drain_scheduler(1000)
            await asyncio.wait_for(cancelled.wait(), timeout=1)


    async def test_escalate_to_owner_calls_local_escalation_without_phone(self) -> None:
        action = insert_action(
            self.db,
            person_id=self.person.id,
            intent="Need approval",
            context="Customer is waiting",
            source="agent",
        )

        with (
            patch("mystic.actions.get_agent_config") as mock_config,
            patch("mystic.calls.initiate_local_escalation", new=AsyncMock(return_value="call-local-123")) as mock_escalation,
        ):
            mock_config.return_value = SimpleNamespace(owner=SimpleNamespace(phone=None))
            escalation_id = await escalate_to_owner(
                self.db,
                action,
                "https://example.test",
                "Owner should handle this personally",
            )

        self.assertIsNotNone(escalation_id)
        mock_escalation.assert_awaited_once()
        assert mock_escalation.await_args is not None
        called_action = mock_escalation.await_args.args[1]
        self.assertIn("Escalation:", called_action.intent)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_sends_desktop_notification(self) -> None:
        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

        with (
            patch("mystic.actions._notifier", ("linux", "/usr/bin/notify-send")),
            patch(
                "mystic.actions.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ) as create_subprocess_exec,
        ):
            sent = await notify("TestBot", "Message from Alice: Please call back")

        self.assertTrue(sent)
        create_subprocess_exec.assert_awaited_once_with(
            "/usr/bin/notify-send",
            "--app-name=mystic-horizon",
            "TestBot",
            "Message from Alice: Please call back",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        process.wait.assert_awaited_once()

    async def test_notify_falls_back_to_logger(self) -> None:
        with (
            patch("mystic.actions._notifier", None),
            patch("mystic.actions.logger.info") as log_info,
            patch("mystic.actions.asyncio.create_subprocess_exec", new=AsyncMock()) as create_subprocess_exec,
        ):
            sent = await notify("TestBot", "Message from Alice: Please call back")

        self.assertFalse(sent)
        log_info.assert_called_once_with(
            "notify.fallback",
            title="TestBot",
            body="Message from Alice: Please call back",
        )
        create_subprocess_exec.assert_not_called()

    async def test_send_email_raises_without_config(self) -> None:
        with patch("mystic.actions.get_smtp_config", return_value=None):
            with self.assertRaises(RuntimeError):
                await send_email("to@example.com", "Sub", "Body")

    async def test_send_email_dispatches_via_to_thread(self) -> None:
        with (
            patch("mystic.actions.get_smtp_config", return_value=_smtp_config()),
            patch("mystic.actions.asyncio.to_thread", new=AsyncMock()) as mock_to_thread,
        ):
            await send_email("to@example.com", "Sub", "Body")
        mock_to_thread.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
