from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from mystic.db import close_database, initialize_schema, insert_call, open_database, upsert_person
from mystic.skills import init_skills, load_handler_module, reset_registry
from mystic.types import OperationalContext
from tests.python_helpers import TempAppHome, seed_core_files

CALL_SID = "CA-hold-001"


class HoldCallSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_registry()
        self.registry = init_skills()
        self.module = load_handler_module(self.registry["hold-call"])

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
            external_id=CALL_SID,
        )
        self.ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )

    def tearDown(self) -> None:
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_default_hold_message_updates_live_call(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(
                self.module,
                "build_authenticated_media_stream_url",
                return_value="wss://test.ts.net/media-stream",
            ),
            patch.object(self.module, "generate_hold_twiml", return_value="<Response />") as hold_twiml,
            patch.object(self.module, "update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            result = await self.module.execute(self.db, self.ctx, {})

        self.assertEqual(result, "Caller is on hold.")
        hold_twiml.assert_called_once_with(
            "Please hold.",
            resume_ws_url="wss://test.ts.net/media-stream",
            resume_params={"callId": self.call.id},
        )
        update_live_call_mock.assert_awaited_once()

    async def test_custom_hold_message_is_passed_to_twiml_builder(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(
                self.module,
                "build_authenticated_media_stream_url",
                return_value="wss://test.ts.net/media-stream",
            ),
            patch.object(self.module, "generate_hold_twiml", return_value="<Response />") as hold_twiml,
            patch.object(self.module, "update_live_call", new=AsyncMock()),
        ):
            await self.module.execute(self.db, self.ctx, {"hold_message": "One moment please."})

        hold_twiml.assert_called_once_with(
            "One moment please.",
            resume_ws_url="wss://test.ts.net/media-stream",
            resume_params={"callId": self.call.id},
        )

    async def test_no_twilio_config_returns_error(self) -> None:
        with patch.object(self.module, "get_twilio_config", return_value=None):
            result = await self.module.execute(self.db, self.ctx, {})

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

        result = await self.module.execute(self.db, local_ctx, {})

        self.assertEqual(result, "This call cannot be placed on hold (local-only).")


if __name__ == "__main__":
    unittest.main()
