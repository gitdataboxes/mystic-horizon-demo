"""Audio codecs, resampling, and call recording helpers."""

from __future__ import annotations

import asyncio
import math
import sys
import wave
from array import array
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from mystic.config import (
    get_agent_config,
    get_error_message,
    get_recordings_dir,
    logger,
)

MULAW_BIAS = 0x84
MULAW_CLIP = 32635


def mulaw_encode(pcm16: Sequence[int]) -> bytes:
    encoded = bytearray(len(pcm16))
    for index, raw_sample in enumerate(pcm16):
        sample = int(raw_sample)
        sign = (sample >> 8) & 0x80
        if sign:
            sample = -sample

        if sample > MULAW_CLIP:
            sample = MULAW_CLIP

        sample += MULAW_BIAS

        exponent = 7
        mask = 0x4000
        for shift in range(7):
            if sample & (mask >> shift):
                break
            exponent -= 1

        mantissa = (sample >> (exponent + 3)) & 0x0F
        encoded[index] = (~(sign | (exponent << 4) | mantissa)) & 0xFF

    return bytes(encoded)


def mulaw_decode(mulaw: bytes | bytearray | memoryview | Sequence[int]) -> list[int]:
    decoded: list[int] = []
    for value in mulaw:
        decoded.append(_DECODE_TABLE[int(value) & 0xFF])
    return decoded


def _decode_sample(index: int) -> int:
    mu = (~index) & 0xFF
    sign = mu & 0x80
    exponent = (mu >> 4) & 0x07
    mantissa = mu & 0x0F
    sample = ((mantissa << 3) + MULAW_BIAS) << exponent
    sample -= MULAW_BIAS
    return -sample if sign else sample


_DECODE_TABLE: tuple[int, ...] = tuple(_decode_sample(index) for index in range(256))


def upsample_8k_to_16k(samples: Sequence[int]) -> list[int]:
    if not samples:
        return []

    output = [0] * (len(samples) * 2)
    for index, sample in enumerate(samples):
        current = int(sample)
        next_sample = int(samples[index + 1]) if index + 1 < len(samples) else current
        output[index * 2] = current
        output[index * 2 + 1] = (current + next_sample) >> 1
    return output


def downsample_16k_to_8k(samples: Sequence[int]) -> list[int]:
    return [int(samples[index]) for index in range(0, len(samples), 2)]


ATTENTION_CUES: tuple[str, ...] = ("Hmm...", "Ahem...", "Um...", "Hey...", "Oh...", "Uh...")

SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
FRAME_DURATION_MS = 20
FRAME_SAMPLES = (SAMPLE_RATE * FRAME_DURATION_MS) // 1000
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE
TOPIC_DISPLAY = "mh.display"
TOPIC_NOTIFY = "mh.notify"
DTMF_FREQUENCIES: dict[str, tuple[int, int]] = {
    "1": (697, 1209),
    "2": (697, 1336),
    "3": (697, 1477),
    "A": (697, 1633),
    "4": (770, 1209),
    "5": (770, 1336),
    "6": (770, 1477),
    "B": (770, 1633),
    "7": (852, 1209),
    "8": (852, 1336),
    "9": (852, 1477),
    "C": (852, 1633),
    "*": (941, 1209),
    "0": (941, 1336),
    "#": (941, 1477),
    "D": (941, 1633),
}


def generate_dtmf_samples(
    digits: str,
    *,
    sample_rate: int = SAMPLE_RATE,
    tone_ms: int = 100,
    pause_ms: int = 100,
    amplitude: float = 0.5,
) -> list[int]:
    if not digits:
        return []

    tone_samples = max(int(sample_rate * tone_ms / 1000), 0)
    pause_samples = max(int(sample_rate * pause_ms / 1000), 0)
    wait_samples = max(int(sample_rate * 500 / 1000), 0)
    clamped_amplitude = max(0.0, min(amplitude, 1.0))

    pcm: list[int] = []
    for raw_digit in digits.upper():
        if raw_digit == "W":
            pcm.extend([0] * wait_samples)
            continue

        freqs = DTMF_FREQUENCIES.get(raw_digit)
        if freqs is None:
            continue

        low_freq, high_freq = freqs
        for index in range(tone_samples):
            time_offset = index / sample_rate
            sample = (
                math.sin(2.0 * math.pi * low_freq * time_offset)
                + math.sin(2.0 * math.pi * high_freq * time_offset)
            ) * 0.5
            scaled = int(round(sample * clamped_amplitude * 32767.0))
            pcm.append(max(-32768, min(32767, scaled)))
        pcm.extend([0] * pause_samples)

    return pcm


def _pcm16le_to_sample_array(data: bytes) -> array[int]:
    samples = array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _sample_array_to_pcm16le(samples: array[int]) -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
        try:
            return samples.tobytes()
        finally:
            samples.byteswap()
    return samples.tobytes()


class CallRecorder:
    """Streams dual-channel PCM16 audio to a WAV file during a call."""

    def __init__(self, call_id: str, recordings_dir: Path) -> None:
        self._call_id = call_id
        self._path = recordings_dir / f"{call_id}.wav"
        self._writer: wave.Wave_write | None = None
        self._lock = asyncio.Lock()
        self._failed = False
        self._caller_pending: deque[bytes] = deque()
        self._agent_pending: deque[bytes] = deque()

    def start(self) -> None:
        if self._writer is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(self._path), "wb")
        writer.setnchannels(2)
        writer.setsampwidth(BYTES_PER_SAMPLE)
        writer.setframerate(SAMPLE_RATE)
        self._writer = writer

    async def write_caller(self, pcm16k: bytes) -> None:
        """Record caller or microphone audio on the left channel."""
        if self._failed or self._writer is None or not pcm16k:
            return
        async with self._lock:
            self._caller_pending.append(pcm16k)
            self._flush()

    async def write_agent(self, pcm16k: bytes) -> None:
        """Record agent audio on the right channel."""
        if self._failed or self._writer is None or not pcm16k:
            return
        async with self._lock:
            self._agent_pending.append(pcm16k)
            self._flush()

    def _flush(self) -> None:
        writer = self._writer
        if writer is None or self._failed:
            return

        while self._caller_pending and self._agent_pending:
            caller = _pcm16le_to_sample_array(self._caller_pending.popleft())
            agent = _pcm16le_to_sample_array(self._agent_pending.popleft())
            stereo = array("h")
            for caller_sample, agent_sample in zip(caller, agent, strict=False):
                stereo.extend((caller_sample, agent_sample))
            try:
                writer.writeframes(_sample_array_to_pcm16le(stereo))
            except OSError as exc:
                logger.warn(
                    "recording.write_failed",
                    callId=self._call_id,
                    error=get_error_message(exc),
                )
                self._failed = True
                return

    def _flush_single(self, pending: deque[bytes], *, left: bool) -> None:
        writer = self._writer
        if writer is None or self._failed:
            return

        channel_index = 0 if left else 1
        while pending:
            samples = _pcm16le_to_sample_array(pending.popleft())
            stereo = array("h", [0] * (len(samples) * 2))
            for index, sample in enumerate(samples):
                stereo[index * 2 + channel_index] = sample
            try:
                writer.writeframes(_sample_array_to_pcm16le(stereo))
            except OSError as exc:
                logger.warn(
                    "recording.write_failed",
                    callId=self._call_id,
                    error=get_error_message(exc),
                )
                self._failed = True
                return

    async def stop(self) -> None:
        writer = self._writer
        if writer is None:
            return

        async with self._lock:
            self._flush_single(self._caller_pending, left=True)
            self._flush_single(self._agent_pending, left=False)

            try:
                writer.close()
            except OSError as exc:
                logger.warn(
                    "recording.close_failed",
                    callId=self._call_id,
                    error=get_error_message(exc),
                )
            finally:
                self._writer = None

            size = self._path.stat().st_size if self._path.exists() else 0
            logger.info(
                "recording.saved",
                callId=self._call_id,
                path=str(self._path),
                bytes=size,
                failed=self._failed,
            )


def start_call_recorder(call_id: str) -> CallRecorder | None:
    if not get_agent_config().recording.enabled:
        return None

    recorder = CallRecorder(call_id, get_recordings_dir())
    try:
        recorder.start()
    except OSError as exc:
        logger.warn(
            "recording.start_failed",
            callId=call_id,
            error=get_error_message(exc),
        )
        return None
    return recorder
