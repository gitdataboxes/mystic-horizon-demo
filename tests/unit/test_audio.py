from __future__ import annotations

import asyncio
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from mystic.config import PocketTtsConfig
from mystic.audio import (
    CallRecorder,
    DTMF_FREQUENCIES,
    downsample_16k_to_8k,
    generate_dtmf_samples,
    mulaw_decode,
    mulaw_encode,
    upsample_8k_to_16k,
)
from mystic.voice import (
    POCKET_CHANNELS,
    PocketChunkedStream,
    PocketTTS,
)


class MuLawTests(unittest.TestCase):
    def test_round_trips_silence(self) -> None:
        decoded = mulaw_decode(mulaw_encode([0]))
        self.assertLess(abs(decoded[0]), 10)

    def test_round_trips_positive_samples_within_tolerance(self) -> None:
        samples = [1000, 5000, 15000, 30000]
        decoded = mulaw_decode(mulaw_encode(samples))

        for original, restored in zip(samples, decoded, strict=True):
            tolerance = max(abs(original) * 0.05, 100)
            self.assertLessEqual(abs(original - restored), tolerance)

    def test_round_trips_negative_samples_within_tolerance(self) -> None:
        samples = [-1000, -5000, -15000, -30000]
        decoded = mulaw_decode(mulaw_encode(samples))

        for original, restored in zip(samples, decoded, strict=True):
            tolerance = max(abs(original) * 0.05, 100)
            self.assertLessEqual(abs(original - restored), tolerance)

    def test_preserves_array_length(self) -> None:
        samples = [int(math.sin((index / 160) * 2 * math.pi) * 10000) for index in range(160)]
        encoded = mulaw_encode(samples)

        self.assertIsInstance(encoded, bytes)
        self.assertEqual(len(encoded), len(samples))
        self.assertEqual(len(mulaw_decode(encoded)), len(samples))

    def test_clips_extreme_values(self) -> None:
        decoded = mulaw_decode(mulaw_encode([32767, -32768]))
        self.assertLessEqual(abs(decoded[0]), 32767)
        self.assertLessEqual(abs(decoded[1]), 32767)

    def test_encoded_bytes_are_eight_bit_values(self) -> None:
        encoded = mulaw_encode([index * 257 - 32768 for index in range(100)])
        self.assertEqual(len(encoded), 100)
        for value in encoded:
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 255)


class ResampleTests(unittest.TestCase):
    def test_upsample_doubles_sample_count(self) -> None:
        self.assertEqual(len(upsample_8k_to_16k([100, 200, 300, 400])), 8)

    def test_upsample_interpolates_between_samples(self) -> None:
        upsampled = upsample_8k_to_16k([0, 1000])
        self.assertEqual(upsampled, [0, 500, 1000, 1000])

    def test_downsample_halves_sample_count(self) -> None:
        self.assertEqual(len(downsample_16k_to_8k([100, 150, 200, 250, 300, 350, 400, 450])), 4)

    def test_downsample_takes_every_other_sample(self) -> None:
        self.assertEqual(downsample_16k_to_8k([100, 150, 200, 250]), [100, 200])

    def test_round_trip_preserves_original_samples(self) -> None:
        original = [500, 1000, 1500, 2000]
        restored = downsample_16k_to_8k(upsample_8k_to_16k(original))
        self.assertEqual(restored, original)

    def test_handles_empty_input(self) -> None:
        self.assertEqual(upsample_8k_to_16k([]), [])
        self.assertEqual(downsample_16k_to_8k([]), [])

    def test_handles_single_sample(self) -> None:
        self.assertEqual(upsample_8k_to_16k([1000]), [1000, 1000])

    def test_preserves_telephony_frame_integrity(self) -> None:
        frame = [int(math.sin((index / 160) * 2 * math.pi) * 10000) for index in range(160)]
        upsampled = upsample_8k_to_16k(frame)
        self.assertEqual(len(upsampled), 320)
        self.assertEqual(downsample_16k_to_8k(upsampled), frame)


class DtmfTests(unittest.TestCase):
    def test_generate_dtmf_samples_single_digit_has_tone_and_pause(self) -> None:
        samples = generate_dtmf_samples("1")
        self.assertEqual(len(samples), 3200)
        self.assertTrue(any(sample != 0 for sample in samples[:1600]))
        self.assertEqual(samples[1600:], [0] * 1600)

    def test_generate_dtmf_samples_multiple_digits_scale_linearly(self) -> None:
        single = generate_dtmf_samples("1")
        sequence = generate_dtmf_samples("123")
        self.assertEqual(len(sequence), len(single) * 3)

    def test_generate_dtmf_samples_empty_input_is_empty(self) -> None:
        self.assertEqual(generate_dtmf_samples(""), [])

    def test_generate_dtmf_samples_skips_invalid_characters(self) -> None:
        self.assertEqual(generate_dtmf_samples("1Z2"), generate_dtmf_samples("12"))

    def test_generate_dtmf_samples_respects_amplitude_without_clipping(self) -> None:
        samples = generate_dtmf_samples("5", amplitude=1.0)
        self.assertLessEqual(max(abs(sample) for sample in samples), 32767)

    def test_generate_dtmf_samples_wait_character_adds_half_second_of_silence(self) -> None:
        samples = generate_dtmf_samples("w")
        self.assertEqual(len(samples), 8000)
        self.assertEqual(samples, [0] * 8000)

    def test_dtmf_frequency_map_contains_standard_digits(self) -> None:
        self.assertEqual(DTMF_FREQUENCIES["1"], (697, 1209))
        self.assertEqual(DTMF_FREQUENCIES["#"], (941, 1477))


def _pcm_bytes(*samples: int) -> bytes:
    return array("h", samples).tobytes()


def _read_pcm_samples(path: Path) -> tuple[wave.Wave_read, list[int]]:
    reader = wave.open(str(path), "rb")
    payload = reader.readframes(reader.getnframes())
    samples = array("h")
    samples.frombytes(payload)
    return reader, list(samples)


class CallRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recordings_dir = Path(tmp) / "nested" / "recordings"
            recorder = CallRecorder("call-123", recordings_dir)

            recorder.start()
            await recorder.stop()

            self.assertTrue((recordings_dir / "call-123.wav").exists())

    async def test_write_caller_and_agent_produces_valid_stereo_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "call-abc.wav"
            recorder = CallRecorder("call-abc", Path(tmp))

            recorder.start()
            await recorder.write_caller(_pcm_bytes(100, -100))
            await recorder.write_agent(_pcm_bytes(200, -200))
            await recorder.stop()

            reader, samples = _read_pcm_samples(path)
            try:
                self.assertEqual(reader.getnchannels(), 2)
                self.assertEqual(reader.getsampwidth(), 2)
                self.assertEqual(reader.getframerate(), 16_000)
                self.assertEqual(samples, [100, 200, -100, -200])
            finally:
                reader.close()

    async def test_stop_pads_unmatched_frames_with_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "call-pad.wav"
            recorder = CallRecorder("call-pad", Path(tmp))

            recorder.start()
            await recorder.write_caller(_pcm_bytes(321, -654))
            await recorder.stop()

            reader, samples = _read_pcm_samples(path)
            try:
                self.assertEqual(samples, [321, 0, -654, 0])
            finally:
                reader.close()

    async def test_stop_without_start_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "call-idle.wav"
            recorder = CallRecorder("call-idle", Path(tmp))

            await recorder.stop()

            self.assertFalse(path.exists())

    async def test_write_failure_sets_failed_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_writer = Mock()
            fake_writer.setnchannels = Mock()
            fake_writer.setsampwidth = Mock()
            fake_writer.setframerate = Mock()
            fake_writer.writeframes.side_effect = OSError("disk full")
            fake_writer.close = Mock()

            with patch("mystic.audio.wave.open", return_value=fake_writer):
                recorder = CallRecorder("call-full", Path(tmp))
                recorder.start()
                await recorder.write_caller(_pcm_bytes(1, 2))
                await recorder.write_agent(_pcm_bytes(3, 4))
                await recorder.stop()

            self.assertTrue(recorder._failed)
            fake_writer.close.assert_called_once()


class _FakeAudioEmitter:
    def __init__(self) -> None:
        self.initialize_calls: list[dict[str, object]] = []
        self.push_calls: list[bytes] = []
        self.flush_count = 0
        self.call_order: list[str] = []

    def initialize(self, **kwargs: object) -> None:
        self.initialize_calls.append(kwargs)
        self.call_order.append("initialize")

    def push(self, data: bytes) -> None:
        self.push_calls.append(data)
        self.call_order.append("push")

    def flush(self) -> None:
        self.flush_count += 1
        self.call_order.append("flush")


class _StreamingEngine:
    SAMPLE_RATE = 24_000

    def __init__(self, chunks: list[object], error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    def stream(self, text: str, *, voice: str) -> object:
        del text, voice
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class PocketChunkedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_streams_chunks_and_flushes_first_audio_early(self) -> None:
        tts = PocketTTS(PocketTtsConfig(provider="pocket"))
        stream = PocketChunkedStream(
            tts=tts,
            input_text="hello there",
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )
        emitter = _FakeAudioEmitter()
        engine = _StreamingEngine(["first", "second"])

        with (
            patch("mystic.voice._get_pocket_engine", return_value={"engine": engine}),
            patch("mystic.voice._to_pcm16_bytes", side_effect=lambda chunk: str(chunk).encode("utf-8")),
            patch("mystic.voice.shortuuid", return_value="req-123"),
        ):
            await stream._run(cast(Any, emitter))

        self.assertEqual(len(emitter.initialize_calls), 1)
        self.assertEqual(
            emitter.initialize_calls[0],
            {
                "request_id": "req-123",
                "sample_rate": engine.SAMPLE_RATE,
                "num_channels": POCKET_CHANNELS,
                "mime_type": "audio/pcm",
            },
        )
        self.assertEqual(emitter.push_calls, [b"first", b"second"])
        self.assertEqual(emitter.flush_count, 2)
        self.assertEqual(
            emitter.call_order,
            ["initialize", "push", "flush", "push", "flush"],
        )

    async def test_run_propagates_stream_errors_without_hanging(self) -> None:
        tts = PocketTTS(PocketTtsConfig(provider="pocket"))
        stream = PocketChunkedStream(
            tts=tts,
            input_text="hello there",
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )
        emitter = _FakeAudioEmitter()
        engine = _StreamingEngine(["partial"], error=RuntimeError("boom"))

        with (
            patch("mystic.voice._get_pocket_engine", return_value={"engine": engine}),
            patch("mystic.voice._to_pcm16_bytes", side_effect=lambda chunk: str(chunk).encode("utf-8")),
            patch("mystic.voice.shortuuid", return_value="req-456"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await asyncio.wait_for(stream._run(cast(Any, emitter)), timeout=1)

        self.assertEqual(emitter.push_calls, [b"partial"])
        self.assertEqual(emitter.flush_count, 1)


if __name__ == "__main__":
    unittest.main()
