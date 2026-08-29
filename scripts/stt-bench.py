#!/usr/bin/env python3
"""Standalone STT mic benchmark — no LiveKit, no LLM, no TTS.

Captures microphone audio via sox, feeds it to Moonshine streaming STT,
and prints transcriptions with wall-clock timing.

Usage:
    .venv/bin/python scripts/stt-bench.py [--model small]

Press Ctrl+C to stop and print a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_DURATION_MS = 20
FRAME_SAMPLES = (SAMPLE_RATE * FRAME_DURATION_MS) // 1000
FRAME_BYTES = FRAME_SAMPLES * 2  # 16-bit


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Moonshine STT mic benchmark")
    parser.add_argument("--model", default="small", choices=["tiny", "small", "medium"])
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    # ── Load Moonshine ────────────────────────────────────────────────
    print(f"Loading Moonshine STT (model={args.model})...")
    load_start = time.monotonic()

    import moonshine_voice
    import numpy as np

    arch_map = {
        "tiny": moonshine_voice.ModelArch.TINY_STREAMING,
        "small": moonshine_voice.ModelArch.SMALL_STREAMING,
        "medium": moonshine_voice.ModelArch.MEDIUM_STREAMING,
    }
    model_path, resolved_arch = moonshine_voice.get_model_for_language("en", arch_map[args.model])
    transcriber = moonshine_voice.Transcriber(model_path=model_path, model_arch=resolved_arch)

    load_elapsed = time.monotonic() - load_start
    print(f"Moonshine loaded in {load_elapsed:.2f}s (arch={resolved_arch.name})")

    # ── State ─────────────────────────────────────────────────────────
    finals: list[dict[str, float | str]] = []
    stream_start = time.monotonic()
    current_line_start: float | None = None
    last_interim = ""

    # ── Listener ──────────────────────────────────────────────────────
    class Listener(moonshine_voice.TranscriptEventListener):
        def on_line_started(self, event: object) -> None:
            nonlocal current_line_start
            current_line_start = time.monotonic()

        def on_line_text_changed(self, event: object) -> None:
            nonlocal last_interim
            text = str(getattr(event, "line", event))
            line_obj = getattr(event, "line", None)
            if line_obj is not None:
                text = str(getattr(line_obj, "text", "") or "").strip()
            if text and text != last_interim:
                elapsed = time.monotonic() - stream_start
                last_interim = text
                sys.stdout.write(f"\r\033[K[{elapsed:6.2f}s] (interim) {text}")
                sys.stdout.flush()

        def on_line_completed(self, event: object) -> None:
            nonlocal current_line_start, last_interim
            line_obj = getattr(event, "line", None)
            text = str(getattr(line_obj, "text", "") or "").strip() if line_obj else ""
            if not text:
                return

            now = time.monotonic()
            elapsed = now - stream_start
            latency = (now - current_line_start) if current_line_start else 0.0
            audio_dur = float(getattr(line_obj, "duration", 0.0) or 0.0)

            finals.append({"text": text, "latency": latency, "audio_duration": audio_dur})
            last_interim = ""
            current_line_start = None
            sys.stdout.write(f"\r\033[K[{elapsed:6.2f}s] {text}  (latency={latency:.3f}s audio={audio_dur:.2f}s)\n")
            sys.stdout.flush()

        def on_error(self, event: object) -> None:
            err = getattr(event, "error", event)
            print(f"\n[error] {err}", file=sys.stderr)

    # ── Start mic ─────────────────────────────────────────────────────
    print("Starting microphone (sox rec)... speak into your mic. Ctrl+C to stop.\n")
    rec_process = await asyncio.create_subprocess_exec(
        "rec", "-q", "--buffer", "1280",
        "-t", "raw", "-r", str(SAMPLE_RATE),
        "-e", "signed-integer", "-b", "16", "-c", str(CHANNELS), "-",
        stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL,
    )
    assert rec_process.stdout is not None

    # ── Stream loop ───────────────────────────────────────────────────
    total_audio_seconds = 0.0
    try:
        with transcriber.create_stream() as stream:
            stream.add_listener(Listener())
            stream.start()

            buf = bytearray()
            while True:
                chunk = await rec_process.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                buf.extend(chunk)

                while len(buf) >= FRAME_BYTES:
                    frame_bytes = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    pcm = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    total_audio_seconds += FRAME_DURATION_MS / 1000.0
                    await asyncio.to_thread(stream.add_audio, pcm, SAMPLE_RATE)

            stream.stop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if rec_process.returncode is None:
            rec_process.terminate()
            try:
                await asyncio.wait_for(rec_process.wait(), timeout=2)
            except asyncio.TimeoutError:
                rec_process.kill()

        transcriber.close()

    # ── Summary ───────────────────────────────────────────────────────
    wall_time = time.monotonic() - stream_start
    print(f"\n{'─' * 60}")
    print(f"Wall time:        {wall_time:.2f}s")
    print(f"Audio processed:  {total_audio_seconds:.2f}s")
    print(f"Final transcripts: {len(finals)}")
    if finals:
        latencies = [f["latency"] for f in finals]
        avg_lat = sum(latencies) / len(latencies)
        max_lat = max(latencies)
        min_lat = min(latencies)
        print(f"Latency (avg):    {avg_lat:.3f}s")
        print(f"Latency (min):    {min_lat:.3f}s")
        print(f"Latency (max):    {max_lat:.3f}s")
        rtf = wall_time / total_audio_seconds if total_audio_seconds > 0 else 0
        print(f"Real-time factor: {rtf:.2f}x")
    print(f"{'─' * 60}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
