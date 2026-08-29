"""Operational handler for send-dtmf."""

from __future__ import annotations

import sqlite3
import struct
from typing import Mapping

from livekit import rtc

from mystic.audio import DTMF_FREQUENCIES, FRAME_DURATION_MS, generate_dtmf_samples
from mystic.db import get_call_by_id
from mystic.types import OperationalContext

_VALID_DTMF_CHARS = frozenset((*DTMF_FREQUENCIES.keys(), "W"))


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    digits = params.get("digits")
    if not isinstance(digits, str) or not digits.strip():
        return "Please provide DTMF digits."

    normalized_digits = digits.strip().upper()
    if any(char not in _VALID_DTMF_CHARS for char in normalized_digits):
        return "Invalid DTMF digits. Use 0-9, *, #, A-D, or w."

    call = get_call_by_id(db, ctx.call_id)
    if call is None or not call.external_id:
        return "This call cannot send DTMF (local-only)."

    audio_source = ctx.audio_source
    if audio_source is None or not hasattr(audio_source, "capture_frame"):
        return "DTMF is only available in an active voice call."

    sample_rate_value = getattr(audio_source, "sample_rate", None)
    sample_rate = sample_rate_value if isinstance(sample_rate_value, int) and sample_rate_value > 0 else 16_000
    samples = generate_dtmf_samples(normalized_digits, sample_rate=sample_rate)
    if not samples:
        return "Please provide DTMF digits."

    frame_samples = max(int(sample_rate * FRAME_DURATION_MS / 1000), 1)
    capture_frame = getattr(audio_source, "capture_frame")
    for start in range(0, len(samples), frame_samples):
        chunk = samples[start:start + frame_samples]
        frame = rtc.AudioFrame(
            struct.pack(f"<{len(chunk)}h", *chunk),
            sample_rate,
            1,
            len(chunk),
        )
        await capture_frame(frame)

    flush = getattr(audio_source, "flush", None)
    if callable(flush):
        flush()

    return f"Sent DTMF: {digits.strip()}"
