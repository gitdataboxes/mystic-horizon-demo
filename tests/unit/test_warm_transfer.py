from __future__ import annotations

import unittest
from unittest.mock import ANY, AsyncMock, patch

from mystic.db import close_database, initialize_schema, insert_call, open_database, upsert_person
from mystic.skills import init_skills, load_handler_module, reset_registry
from mystic.types import OperationalContext
from tests.python_helpers import TempAppHome, seed_core_files

OWNER_PHONE = "+15551234567"
CALL_SID = "CA-warm-001"


class WarmTransferSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_registry()
        self.registry = init_skills()
        self.module = load_handler_module(self.registry["warm-transfer-call"])

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
            external_id=CALL_SID,
        )
        self.owner_ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )
        self.public_ctx = OperationalContext(
            audience="public",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )

    def tearDown(self) -> None:
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_owner_warm_transfer_holds_calls_target_and_moves_caller_to_conference(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(
                self.module,
                "build_authenticated_media_stream_url",
                return_value="wss://test.ts.net/media-stream",
            ),
            patch.object(self.module, "generate_hold_twiml", return_value="<Hold />") as hold_twiml,
            patch.object(self.module, "generate_say_conference_twiml", return_value="<Target />") as target_twiml,
            patch.object(self.module, "generate_conference_twiml", return_value="<Conference />") as conference_twiml,
            patch.object(self.module, "make_outbound_call", new=AsyncMock(return_value="CA-target-001")) as outbound_call,
            patch.object(self.module, "update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            result = await self.module.execute(
                self.db,
                self.owner_ctx,
                {"destination": "+15550002222", "introduction": "You have Alice on the line."},
            )

        self.assertEqual(result, "Transferring call to +15550002222 with introduction.")
        hold_twiml.assert_called_once_with(
            "Please hold while I connect your call.",
            resume_ws_url="wss://test.ts.net/media-stream",
            resume_params={"callId": self.call.id},
        )
        target_twiml.assert_called_once_with(
            "You have Alice on the line.",
            f"transfer-{self.call.id}",
            end_on_exit=True,
        )
        outbound_call.assert_awaited_once_with(
            ANY,
            "+15550002222",
            "<Target />",
            status_callback=f"https://test.ts.net/webhook/twilio/status?callerCallId={self.call.id}",
        )
        conference_twiml.assert_called_once_with(
            f"transfer-{self.call.id}",
            action=f"https://test.ts.net/webhook/twilio/dial-action?callId={self.call.id}&reconnect=1",
        )
        self.assertEqual(update_live_call_mock.await_count, 2)

    async def test_destination_owner_resolves_to_owner_phone(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(
                self.module,
                "build_authenticated_media_stream_url",
                return_value="wss://test.ts.net/media-stream",
            ),
            patch.object(self.module, "generate_hold_twiml", return_value="<Hold />"),
            patch.object(self.module, "generate_say_conference_twiml", return_value="<Target />"),
            patch.object(self.module, "generate_conference_twiml", return_value="<Conference />"),
            patch.object(self.module, "make_outbound_call", new=AsyncMock(return_value="CA-target-001")) as outbound_call,
            patch.object(self.module, "update_live_call", new=AsyncMock()),
        ):
            result = await self.module.execute(self.db, self.owner_ctx, {"destination": "owner"})

        self.assertEqual(result, f"Transferring call to {OWNER_PHONE} with introduction.")
        outbound_call.assert_awaited_once()
        assert outbound_call.await_args is not None
        self.assertEqual(outbound_call.await_args.args[1], OWNER_PHONE)

    async def test_public_caller_is_rejected(self) -> None:
        result = await self.module.execute(self.db, self.public_ctx, {"destination": "+15550002222"})
        self.assertEqual(result, "Only the owner can warm-transfer calls.")

    async def test_no_twilio_config_returns_error(self) -> None:
        with patch.object(self.module, "get_twilio_config", return_value=None):
            result = await self.module.execute(self.db, self.owner_ctx, {"destination": "+15550002222"})

        self.assertEqual(result, "Twilio is not configured. Run init --connect-twilio first.")

    async def test_local_only_call_returns_error(self) -> None:
        local_call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
        )
        local_ctx = OperationalContext(
            audience="owner",
            call_id=local_call.id,
            person_id=self.person.id,
            source="mid-call",
        )

        result = await self.module.execute(self.db, local_ctx, {"destination": "+15550002222"})

        self.assertEqual(result, "This call cannot be warm-transferred (local-only).")

    async def test_default_introduction_is_used_when_missing(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(
                self.module,
                "build_authenticated_media_stream_url",
                return_value="wss://test.ts.net/media-stream",
            ),
            patch.object(self.module, "generate_hold_twiml", return_value="<Hold />"),
            patch.object(self.module, "generate_say_conference_twiml", return_value="<Target />") as target_twiml,
            patch.object(self.module, "generate_conference_twiml", return_value="<Conference />"),
            patch.object(self.module, "make_outbound_call", new=AsyncMock(return_value="CA-target-001")),
            patch.object(self.module, "update_live_call", new=AsyncMock()),
        ):
            await self.module.execute(self.db, self.owner_ctx, {"destination": "+15550002222"})

        target_twiml.assert_called_once_with(
            "You have a call being transferred to you.",
            f"transfer-{self.call.id}",
            end_on_exit=True,
        )


if __name__ == "__main__":
    unittest.main()
