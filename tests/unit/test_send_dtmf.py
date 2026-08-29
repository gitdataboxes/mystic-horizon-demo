from __future__ import annotations

import unittest
from unittest.mock import patch

from mystic.db import close_database, initialize_schema, insert_call, open_database, upsert_person
from mystic.skills import init_skills, load_handler_module, reset_registry
from mystic.types import OperationalContext
from tests.python_helpers import TempAppHome, seed_core_files


class _FakeAudioSource:
    def __init__(self, sample_rate: int = 16_000) -> None:
        self.sample_rate = sample_rate
        self.frames: list[object] = []
        self.flush_count = 0

    async def capture_frame(self, frame: object) -> None:
        self.frames.append(frame)

    def flush(self) -> None:
        self.flush_count += 1


class SendDtmfSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_registry()
        self.registry = init_skills()
        self.module = load_handler_module(self.registry["send-dtmf"])

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="outbound",
            audience="owner",
            external_id="CA-dtmf-001",
        )

    def tearDown(self) -> None:
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_valid_digits_are_generated_and_pushed_as_frames(self) -> None:
        audio_source = _FakeAudioSource()
        ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
            audio_source=audio_source,
        )

        with patch.object(self.module, "generate_dtmf_samples", return_value=[100] * 640):
            result = await self.module.execute(self.db, ctx, {"digits": "12"})

        self.assertEqual(result, "Sent DTMF: 12")
        self.assertEqual(len(audio_source.frames), 2)
        self.assertEqual(audio_source.flush_count, 1)

    async def test_empty_digits_return_error(self) -> None:
        ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
            audio_source=_FakeAudioSource(),
        )

        result = await self.module.execute(self.db, ctx, {"digits": ""})

        self.assertEqual(result, "Please provide DTMF digits.")

    async def test_invalid_digits_return_error(self) -> None:
        ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
            audio_source=_FakeAudioSource(),
        )

        result = await self.module.execute(self.db, ctx, {"digits": "1Z2"})

        self.assertEqual(result, "Invalid DTMF digits. Use 0-9, *, #, A-D, or w.")

    async def test_missing_audio_source_returns_error(self) -> None:
        ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )

        result = await self.module.execute(self.db, ctx, {"digits": "123"})

        self.assertEqual(result, "DTMF is only available in an active voice call.")

    async def test_local_only_call_returns_error(self) -> None:
        local_call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="outbound",
            audience="owner",
        )
        ctx = OperationalContext(
            audience="owner",
            call_id=local_call.id,
            person_id=self.person.id,
            source="mid-call",
            audio_source=_FakeAudioSource(),
        )

        result = await self.module.execute(self.db, ctx, {"digits": "123"})

        self.assertEqual(result, "This call cannot send DTMF (local-only).")


if __name__ == "__main__":
    unittest.main()
