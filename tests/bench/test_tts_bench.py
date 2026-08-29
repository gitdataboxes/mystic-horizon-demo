"""Benchmarks for Pocket TTS first-byte latency with the warmed ONNX engine."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from mystic.config import PocketTtsConfig
from mystic.voice import PocketTTS, _get_pocket_engine, pocket_onnx_models_missing

_SKIP_REASON = "Pocket TTS ONNX models not downloaded (run 'mystic-horizon init')"
_BENCH_TEXT = "Let's confirm the Tuesday afternoon follow-up and keep the budget packet ready."
_HAS_MODELS = not pocket_onnx_models_missing()


class _FirstChunkEmitter:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.first_chunk = loop.create_future()

    def initialize(self, **_kwargs: object) -> None:
        return None

    def push(self, data: bytes) -> None:
        if data and not self.first_chunk.done():
            self.first_chunk.set_result(None)

    def flush(self) -> None:
        return None


def _run_async(
    loop: asyncio.AbstractEventLoop,
    async_fn: Any,
    *args: object,
) -> object:
    return loop.run_until_complete(async_fn(*args))


async def _measure_first_chunk(tts: PocketTTS) -> None:
    stream = tts.synthesize(_BENCH_TEXT)
    emitter = _FirstChunkEmitter(asyncio.get_running_loop())
    task = asyncio.create_task(stream._run(emitter))  # type: ignore[arg-type]  # bench stub
    try:
        await asyncio.wait_for(emitter.first_chunk, timeout=10)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.bench
@pytest.mark.skipif(not _HAS_MODELS, reason=_SKIP_REASON)
class TestPocketTtsBench:
    def test_first_chunk_latency(self, benchmark) -> None:
        tts = PocketTTS(PocketTtsConfig(provider="pocket"))
        _get_pocket_engine(tts.model)

        loop = asyncio.new_event_loop()
        try:
            benchmark(_run_async, loop, _measure_first_chunk, tts)
        finally:
            loop.close()
