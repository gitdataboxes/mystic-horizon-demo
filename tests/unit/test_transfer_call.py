from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mystic.config import get_twilio_config
from mystic.db import close_database, initialize_schema, insert_call, open_database, upsert_person
from mystic.skills import init_skills, load_handler_module, reset_registry
from mystic.types import OperationalContext
from tests.python_helpers import TempAppHome, seed_core_files

OWNER_PHONE = "+15551234567"
TRANSFER_SID = "CA-transfer-001"


class TransferCallSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_registry()
        self.registry = init_skills()
        self.module = load_handler_module(self.registry["transfer-call"])

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
            external_id=TRANSFER_SID,
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

    async def test_owner_transfer_updates_live_call(self) -> None:
        config = get_twilio_config()
        self.assertIsNotNone(config)
        assert config is not None

        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(self.module, "generate_dial_twiml", return_value="<Response />") as dial_twiml,
            patch.object(self.module, "update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            result = await self.module.execute(
                self.db,
                self.owner_ctx,
                {"destination": "+15550002222"},
            )

        self.assertEqual(result, "Transferring call to +15550002222.")
        dial_twiml.assert_called_once_with(
            "+15550002222",
            caller_id=config.phoneNumber,
            action=f"https://test.ts.net/webhook/twilio/dial-action?callId={self.call.id}",
        )
        update_live_call_mock.assert_awaited_once_with(
            config,
            TRANSFER_SID,
            twiml="<Response />",
        )

    async def test_destination_owner_resolves_to_owner_phone(self) -> None:
        config = get_twilio_config()
        self.assertIsNotNone(config)
        assert config is not None

        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(self.module, "generate_dial_twiml", return_value="<Response />") as dial_twiml,
            patch.object(self.module, "update_live_call", new=AsyncMock()),
        ):
            result = await self.module.execute(self.db, self.owner_ctx, {"destination": "owner"})

        self.assertEqual(result, f"Transferring call to {OWNER_PHONE}.")
        dial_twiml.assert_called_once_with(
            OWNER_PHONE,
            caller_id=config.phoneNumber,
            action=f"https://test.ts.net/webhook/twilio/dial-action?callId={self.call.id}",
        )

    async def test_destination_owner_without_configured_phone_returns_error(self) -> None:
        fake_agent_config = SimpleNamespace(owner=SimpleNamespace(phone=None))
        with patch.object(self.module, "get_agent_config", return_value=fake_agent_config):
            result = await self.module.execute(self.db, self.owner_ctx, {"destination": "owner"})

        self.assertEqual(result, "Owner phone is not configured.")

    async def test_public_caller_cannot_transfer_to_arbitrary_number(self) -> None:
        result = await self.module.execute(
            self.db,
            self.public_ctx,
            {"destination": "+15550002222"},
        )

        self.assertEqual(result, "Public callers can only transfer to the owner.")

    async def test_public_caller_can_transfer_to_owner(self) -> None:
        with (
            patch.object(self.module, "get_tunnel_url", return_value="https://test.ts.net"),
            patch.object(self.module, "generate_dial_twiml", return_value="<Response />") as dial_twiml,
            patch.object(self.module, "update_live_call", new=AsyncMock()) as update_live_call_mock,
        ):
            result = await self.module.execute(self.db, self.public_ctx, {"destination": "owner"})

        self.assertEqual(result, f"Transferring call to {OWNER_PHONE}.")
        dial_twiml.assert_called_once()
        update_live_call_mock.assert_awaited_once()

    async def test_no_twilio_config_returns_error(self) -> None:
        with patch.object(self.module, "get_twilio_config", return_value=None):
            result = await self.module.execute(self.db, self.owner_ctx, {"destination": "owner"})

        self.assertEqual(result, "Twilio is not configured. Run init --connect-twilio first.")

    async def test_local_only_call_cannot_be_transferred(self) -> None:
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

        result = await self.module.execute(
            self.db,
            local_ctx,
            {"destination": "+15550002222"},
        )

        self.assertEqual(result, "This call cannot be transferred (local-only).")

    async def test_missing_or_empty_destination_returns_error(self) -> None:
        for params in ({}, {"destination": ""}, {"destination": "   "}):
            with self.subTest(params=params):
                result = await self.module.execute(self.db, self.owner_ctx, params)
                self.assertEqual(result, "Please provide a transfer destination.")


if __name__ == "__main__":
    unittest.main()
