"""Benchmark TTFT (time to first token) for OpenRouter models.

Requires a live OpenRouter API key in the agent's providers.json.
Run with:  .venv/bin/python -m pytest tests/bench/test_llm_ttft_bench.py -s
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time

import httpx
import pytest

from mystic.config import OPENROUTER_BASE_URL
from mystic.llm import build_llm_headers

MODELS = [
    "openai/gpt-4o",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.4",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.6",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash",
    "mistralai/mistral-small-2603",
    "mistralai/mistral-medium-3.1",
    "deepseek/deepseek-v3.2",
    "z-ai/glm-5-turbo",
    "z-ai/glm-5",
    "moonshotai/kimi-k2.5",
]

SYSTEM_PROMPT = (
    "You're on a phone call. Speak like a real person:\n"
    "- Use casual language, conversational fillers: \"Umm...\", \"Well...\", \"I mean...\", \"So like...\"\n"
    "- Keep your turns SHORT. One or two sentences, then pause for them to respond.\n"
    "- One question at a time. Never list multiple questions."
)
PROMPT = "Write a witty aphorism."
RUNS_PER_MODEL = 1
WARMUP_RUNS = 0


async def _measure_ttft(
    api_key: str,
    model: str,
    *,
    base_url: str = OPENROUTER_BASE_URL,
) -> tuple[float, float, int, str]:
    """Return (ttft_ms, total_ms, token_count, full_text) for one streaming request."""
    headers = build_llm_headers(api_key, is_openrouter="openrouter.ai" in base_url)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": PROMPT}],
        "stream": True,
        "max_tokens": 80,
    }

    token_count = 0
    ttft: float | None = None
    chunks: list[str] = []
    t0 = time.perf_counter()

    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        ) as response:
            if not 200 <= response.status_code < 300:
                raw = await response.aread()
                raise RuntimeError(f"{model}: HTTP {response.status_code} — {raw.decode()[:200]}")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                    delta = event["choices"][0]["delta"].get("content") or ""
                    if delta:
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
                        token_count += 1
                        chunks.append(delta)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    total = (time.perf_counter() - t0) * 1000
    return (ttft or total, total, token_count, "".join(chunks))


def _get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        from mystic.config import get_realtime_llm_config
        cfg = get_realtime_llm_config()
        if cfg.apiKey:
            return cfg.apiKey
    except (FileNotFoundError, ValueError):
        pass
    pytest.skip("Set OPENROUTER_API_KEY or configure providers.json")


async def _stream_response(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
) -> tuple[float, float, int, str]:
    """Stream one request, return (ttft_ms, total_ms, token_count, text)."""
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 120,
    }
    token_count = 0
    ttft: float | None = None
    chunks: list[str] = []
    t0 = time.perf_counter()

    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json=body,
    ) as response:
        if not 200 <= response.status_code < 300:
            raw = await response.aread()
            raise RuntimeError(f"{model}: HTTP {response.status_code} — {raw.decode()[:200]}")
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                break
            try:
                event = json.loads(payload)
                delta = event["choices"][0]["delta"].get("content") or ""
                if delta:
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
                    token_count += 1
                    chunks.append(delta)
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

    total = (time.perf_counter() - t0) * 1000
    return (ttft or total, total, token_count, "".join(chunks))


async def _measure_conversation(
    api_key: str,
    model: str,
    *,
    base_url: str = OPENROUTER_BASE_URL,
) -> tuple[float, str, float, str]:
    """Two-turn conversation. Returns (ttft1, text1, ttft2, text2)."""
    headers = build_llm_headers(api_key, is_openrouter="openrouter.ai" in base_url)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": PROMPT},
    ]

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Turn 1
        ttft1, _, _, text1 = await _stream_response(client, headers, base_url, model, messages)

        # Build turn 2
        messages.append({"role": "assistant", "content": text1})
        messages.append({"role": "user", "content": "Interesting."})

        ttft2, _, _, text2 = await _stream_response(client, headers, base_url, model, messages)

    return ttft1, text1, ttft2, text2


@pytest.mark.timeout(180)
def test_ttft_benchmark():
    """Measure TTFT across models and print a summary table."""
    api_key = _get_api_key()
    results: dict[str, list[float]] = {}

    async def run_all():
        for model in MODELS:
            ttfts: list[float] = []

            # warmup
            for _ in range(WARMUP_RUNS):
                try:
                    await _measure_ttft(api_key, model)
                except RuntimeError as exc:
                    print(f"\n  SKIP {model}: {exc}")
                    break
            else:
                # measurement runs
                for i in range(RUNS_PER_MODEL):
                    try:
                        ttft, total, tokens, text = await _measure_ttft(api_key, model)
                        ttfts.append(ttft)
                        print(f"  {model} run {i+1}: TTFT={ttft:.0f}ms  total={total:.0f}ms  tokens={tokens}")
                        print(f"    → {text.strip()}")
                    except RuntimeError as exc:
                        print(f"  {model} run {i+1}: ERROR — {exc}")

            if ttfts:
                results[model] = ttfts

    asyncio.run(run_all())

    # summary
    print("\n" + "=" * 72)
    print(f"{'Model':<28} {'Med TTFT':>10} {'Mean TTFT':>10} {'Min':>8} {'Max':>8} {'StdDev':>8}")
    print("-" * 72)
    for model, ttfts in results.items():
        med = statistics.median(ttfts)
        mean = statistics.mean(ttfts)
        lo = min(ttfts)
        hi = max(ttfts)
        sd = statistics.stdev(ttfts) if len(ttfts) > 1 else 0.0
        print(f"{model:<28} {med:>8.0f}ms {mean:>8.0f}ms {lo:>6.0f}ms {hi:>6.0f}ms {sd:>6.0f}ms")
    print("=" * 72)


@pytest.mark.timeout(180)
def test_conversation_benchmark():
    """Two-turn conversation: aphorism → 'Interesting.' — measure both TTFTs."""
    api_key = _get_api_key()

    async def run_all():
        for model in MODELS:
            print(f"\n  {model}")
            try:
                ttft1, text1, ttft2, text2 = await _measure_conversation(api_key, model)
                print(f"    Turn 1 TTFT: {ttft1:.0f}ms")
                print(f"      → {text1.strip()}")
                print(f"    Turn 2 TTFT: {ttft2:.0f}ms")
                print(f"      → {text2.strip()}")
            except (RuntimeError, httpx.ReadTimeout) as exc:
                print(f"    ERROR — {exc}")

    asyncio.run(run_all())
