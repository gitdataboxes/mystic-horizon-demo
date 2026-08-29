from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from mystic.config import (
    DeepgramSttConfig,
    InworldTtsConfig,
    MoonshineSttConfig,
    PocketTtsConfig,
    ResolvedLLMConfig,
)
from mystic.latency import build_provider_probes, collect_provider_latency


class ProviderLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_provider_latency_uses_remote_cloud_providers(self) -> None:
        stt = DeepgramSttConfig(provider="deepgram", apiKey="dg")
        llm = ResolvedLLMConfig(
            baseURL="https://openrouter.ai/api/v1",
            apiKey="or",
            model="openai/gpt-5.4-mini",
        )
        tts = InworldTtsConfig(provider="inworld", apiKey="iw")

        with patch("mystic.latency._measure_connect_latency", new=AsyncMock(return_value=42.4)):
            payload = await collect_provider_latency(stt, llm, tts)

        assert payload is not None
        samples = payload["samples"]
        self.assertEqual(samples["stt"]["provider"], "Deepgram")
        self.assertEqual(samples["llm"]["provider"], "OpenRouter")
        self.assertEqual(samples["tts"]["provider"], "Inworld")
        self.assertEqual(samples["stt"]["latencyMs"], 42)
        self.assertEqual(samples["llm"]["status"], "ok")
        self.assertEqual(samples["tts"]["status"], "ok")

    async def test_collect_provider_latency_skips_all_local_providers(self) -> None:
        stt = MoonshineSttConfig(provider="moonshine", model="small")
        llm = ResolvedLLMConfig(baseURL="http://localhost:11434/v1", apiKey=None, model="local")
        tts = PocketTtsConfig(provider="pocket")

        payload = await collect_provider_latency(stt, llm, tts)

        self.assertIsNone(payload)

    def test_build_provider_probes_marks_local_and_remote_slots(self) -> None:
        probes = build_provider_probes(
            MoonshineSttConfig(provider="moonshine", model="small"),
            ResolvedLLMConfig(baseURL="https://openrouter.ai/api/v1", apiKey="or", model="m"),
            PocketTtsConfig(provider="pocket"),
        )

        self.assertEqual(probes["stt"].status, "local")
        self.assertEqual(probes["llm"].status, "probe")
        self.assertEqual(probes["llm"].provider, "OpenRouter")
        self.assertEqual(probes["tts"].status, "local")
