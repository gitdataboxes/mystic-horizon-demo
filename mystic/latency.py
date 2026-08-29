"""Network-only provider latency probes for dashboard HUD telemetry."""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

from mystic.config import (
    DeepgramSttConfig,
    InworldTtsConfig,
    ResolvedLLMConfig,
    SttConfig,
    TtsConfig,
)

ProviderSlot = Literal["stt", "llm", "tts"]
LatencyPublisher = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_LATENCY_PROBE_INTERVAL_SECONDS = 5.0
DEFAULT_LATENCY_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True, frozen=True)
class ProviderProbe:
    slot: ProviderSlot
    provider: str
    url: str | None = None
    status: Literal["probe", "local", "unconfigured"] = "probe"


def build_provider_probes(
    stt_config: SttConfig,
    llm_config: ResolvedLLMConfig,
    tts_config: TtsConfig,
) -> dict[ProviderSlot, ProviderProbe]:
    return {
        "stt": _stt_probe(stt_config),
        "llm": _llm_probe(llm_config),
        "tts": _tts_probe(tts_config),
    }


async def collect_provider_latency(
    stt_config: SttConfig,
    llm_config: ResolvedLLMConfig,
    tts_config: TtsConfig,
    *,
    timeout_seconds: float = DEFAULT_LATENCY_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    probes = build_provider_probes(stt_config, llm_config, tts_config)
    if not any(probe.status == "probe" for probe in probes.values()):
        return None

    sampled_at = int(time.time() * 1000)
    results = await asyncio.gather(
        *(_sample_probe(probe, timeout_seconds=timeout_seconds) for probe in probes.values())
    )
    return {
        "sampledAt": sampled_at,
        "samples": {probe.slot: sample for probe, sample in zip(probes.values(), results)},
    }


async def publish_provider_latency_loop(
    publish: LatencyPublisher,
    stt_config: SttConfig,
    llm_config: ResolvedLLMConfig,
    tts_config: TtsConfig,
    *,
    interval_seconds: float = DEFAULT_LATENCY_PROBE_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_LATENCY_PROBE_TIMEOUT_SECONDS,
) -> None:
    while True:
        payload = await collect_provider_latency(
            stt_config,
            llm_config,
            tts_config,
            timeout_seconds=timeout_seconds,
        )
        if payload is None:
            return
        await publish({"type": "provider_latency", **payload})
        await asyncio.sleep(interval_seconds)


def _stt_probe(config: SttConfig) -> ProviderProbe:
    provider = str(getattr(config, "provider", "") or "").strip().lower()
    if not provider:
        return ProviderProbe(slot="stt", provider="STT", status="unconfigured")
    if isinstance(config, DeepgramSttConfig) or provider == "deepgram":
        return ProviderProbe(slot="stt", provider="Deepgram", url="https://api.deepgram.com/v1/listen")
    return ProviderProbe(slot="stt", provider=provider.title(), status="local")


def _llm_probe(config: ResolvedLLMConfig) -> ProviderProbe:
    base_url = str(getattr(config, "baseURL", "") or "").strip()
    if not base_url:
        return ProviderProbe(slot="llm", provider="LLM", status="unconfigured")
    provider = "OpenRouter" if "openrouter.ai" in base_url.lower() else "LLM"
    if _is_local_url(base_url):
        return ProviderProbe(slot="llm", provider=provider, status="local")
    return ProviderProbe(slot="llm", provider=provider, url=base_url)


def _tts_probe(config: TtsConfig) -> ProviderProbe:
    provider = str(getattr(config, "provider", "") or "").strip().lower()
    if not provider:
        return ProviderProbe(slot="tts", provider="TTS", status="unconfigured")
    if isinstance(config, InworldTtsConfig) or provider == "inworld":
        return ProviderProbe(slot="tts", provider="Inworld", url="wss://api.inworld.ai/")
    return ProviderProbe(slot="tts", provider=provider.title(), status="local")


async def _sample_probe(
    probe: ProviderProbe,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    sample: dict[str, Any] = {"provider": probe.provider}
    if probe.status != "probe" or probe.url is None:
        sample["status"] = probe.status
        sample["latencyMs"] = None
        return sample

    endpoint = _endpoint_from_url(probe.url)
    sample["endpoint"] = endpoint["host"]
    try:
        latency_ms = await _measure_connect_latency(
            endpoint["host"],
            endpoint["port"],
            use_tls=endpoint["use_tls"],
            timeout_seconds=timeout_seconds,
        )
    except TimeoutError:
        sample["status"] = "timeout"
        sample["latencyMs"] = None
    except (OSError, ssl.SSLError, socket.gaierror):
        sample["status"] = "error"
        sample["latencyMs"] = None
    else:
        sample["status"] = "ok"
        sample["latencyMs"] = round(latency_ms)
    return sample


async def _measure_connect_latency(
    host: str,
    port: int,
    *,
    use_tls: bool,
    timeout_seconds: float,
) -> float:
    context = ssl.create_default_context() if use_tls else None
    started = time.perf_counter()
    connect = asyncio.open_connection(
        host,
        port,
        ssl=context,
        server_hostname=host if context is not None else None,
    )
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(connect, timeout=timeout_seconds)
        return (time.perf_counter() - started) * 1000
    except asyncio.TimeoutError as exc:
        raise TimeoutError from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        _ = reader


def _endpoint_from_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname or url
    port = parsed.port
    if port is None:
        port = 443 if scheme in {"https", "wss"} else 80
    return {"host": host, "port": port, "use_tls": scheme in {"https", "wss"}}


def _is_local_url(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")
