"""Voice pipeline core plus compatibility re-exports for moved audio/worker APIs."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterable, Awaitable, Callable, Literal, Mapping
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

from livekit import rtc
from livekit.agents import (
    APIConnectOptions,
    Agent,
    LanguageCode,
    RunContext,
    function_tool,
    llm,
    stt as agents_stt,
    tts as agents_tts,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer
from livekit.agents.utils import shortuuid
from livekit.plugins import openai, silero

from mystic.audio import (
    ATTENTION_CUES, MULAW_BIAS, MULAW_CLIP, _DECODE_TABLE,
    _decode_sample,
    downsample_16k_to_8k,
    mulaw_decode, mulaw_encode, upsample_8k_to_16k,
)
from mystic.config import (
    DeepgramSttConfig,
    InworldTtsConfig,
    MoonshineSttConfig,
    PocketTtsConfig,
    ResolvedLLMConfig,
    SttConfig,
    TtsConfig,
    UnconfiguredSttConfig,
    UnconfiguredTtsConfig,
    get_agent_config,
    get_error_message,
    get_shared_home,
    logger,
)
from mystic.db import now_ms as _db_now_ms
from mystic.skills import (
    build_tools_for_context,
    execute_tool as _skills_execute_tool,
    get_registry,
    init_skills,
)
from mystic.types import Modality, SkillContext


@dataclass(slots=True, frozen=True)
class TranscriptEntry:
    role: str
    text: str
    timestamp: int
    modality: Literal["voice", "text", "tool"]


class TranscriptCollector:
    def __init__(self, now_ms: Callable[[], int] | None = None) -> None:
        self._now_ms = now_ms or _db_now_ms
        self._entries: list[TranscriptEntry] = []
        self._persisted_up_to = 0
        self._start_time = self._now_ms()
        self._last_agent_signature: tuple[str, str] = ("", "")
        self._last_user_signature: tuple[str, str] = ("", "")

    def add_agent_speech(self, text: str, modality: Literal["voice", "text"] = "voice") -> None:
        self._add_speech("agent", text, modality)

    def add_user_speech(self, text: str, modality: Literal["voice", "text"] = "voice") -> None:
        self._add_speech("user", text, modality)

    def add_tool_event(
        self,
        event_type: str,
        tool_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        event_type = event_type.strip()
        tool_name = tool_name.strip() or "tool"
        if event_type not in {"tool_started", "tool_completed"}:
            return

        event: dict[str, object] = {"type": event_type, "name": tool_name}
        if payload:
            for key in ("args_summary", "duration_ms", "error"):
                if key in payload:
                    event[key] = payload[key]

        self._entries.append(
            TranscriptEntry(
                role="tool",
                text=json.dumps(event, separators=(",", ":")),
                timestamp=self._now_ms(),
                modality="tool",
            )
        )

    def get_entries(self) -> list[TranscriptEntry]:
        return list(self._entries)

    def peek_delta_transcript(self) -> str:
        return self._format_range(self._persisted_up_to, len(self._entries))

    def consume_delta_transcript(self) -> str:
        result = self._format_range(self._persisted_up_to, len(self._entries))
        self._persisted_up_to = len(self._entries)
        return result

    def to_transcript(self) -> str:
        return self._format_range(0, len(self._entries))

    def get_duration(self) -> int:
        return round((self._now_ms() - self._start_time) / 1000)

    def _add_speech(
        self,
        role: str,
        text: str,
        modality: Literal["voice", "text"],
    ) -> None:
        trimmed = text.strip()
        if not trimmed:
            return

        signature = (trimmed, modality)
        if role == "agent" and signature == self._last_agent_signature:
            return
        if role == "user" and signature == self._last_user_signature:
            return

        if role == "agent":
            self._last_agent_signature = signature
        else:
            self._last_user_signature = signature

        self._entries.append(
            TranscriptEntry(
                role=role,
                text=trimmed,
                timestamp=self._now_ms(),
                modality=modality,
            )
        )

    def _format_range(self, start: int, end: int) -> str:
        if start >= end:
            return ""
        entries = sorted(self._entries[start:end], key=lambda entry: entry.timestamp)
        return "\n".join(self._format_entry(entry) for entry in entries)

    def _format_entry(self, entry: TranscriptEntry) -> str:
        elapsed_seconds = max(0, (entry.timestamp - self._start_time) // 1000)
        minutes, seconds = divmod(elapsed_seconds, 60)
        if entry.role == "tool":
            return f"[{minutes}:{seconds:02d}] Tool [event]: {entry.text}"
        label = "Agent" if entry.role == "agent" else "Caller"
        if entry.modality != "voice":
            label = f"{label} [{entry.modality}]"
        return f"[{minutes}:{seconds:02d}] {label}: {entry.text}"


def create_transcript_collector(now_ms: Callable[[], int] | None = None) -> TranscriptCollector:
    return TranscriptCollector(now_ms)


MOONSHINE_SAMPLE_RATE = 16_000
DEFAULT_VOICE = "Hades"
DEFAULT_LLM_MAX_COMPLETION_TOKENS = 4096
TTS_SAMPLE_RATE = 24_000
POCKET_CHANNELS = 1
POCKET_ONNX_REVISION = "4bd665d8a6c8a0cff125fc3196aeecb0f3ae33f9"
POCKET_ONNX_BASE = f"https://huggingface.co/KevinAHM/pocket-tts-onnx/resolve/{POCKET_ONNX_REVISION}"
POCKET_ONNX_FILES = (
    "onnx/flow_lm_main.onnx",
    "onnx/flow_lm_flow.onnx",
    "onnx/mimi_decoder.onnx",
    "onnx/mimi_encoder.onnx",
    "onnx/text_conditioner.onnx",
    "tokenizer.model",
)
_POCKET_ENGINE_CACHE: dict[str, dict[str, Any]] = {}
_POCKET_ENGINE_LOCK = threading.Lock()
_VAD_INSTANCE: object | None = None
_VAD_LOCK = asyncio.Lock()


@dataclass(slots=True, frozen=True)
class PipelineConfig:
    stt: SttConfig
    tts: TtsConfig
    llm: ResolvedLLMConfig


_TOOL_NARRATION = (
    "\n\n## Tool Use\n\n"
    "When you need to use a tool, say what you're doing so the caller isn't left "
    "in silence. Briefly describe what you're looking up or doing before calling "
    "the tool, then transition naturally when sharing results. For example: "
    '"Let me check on that... okay, here\'s what I found."'
)

_TIME_MARKER_PREFIX = "mh-time-"
_DUPLICATE_BLOCK_MIN_CHARS = 12
_BLANK_LINE_RE = re.compile(r"(\n[ \t]*\n[ \t]*)")


def _normalize_duplicate_block(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def collapse_adjacent_repeated_text(text: str) -> str:
    """Collapse accidental adjacent duplicate paragraphs from streamed LLM text."""
    if not text:
        return text

    pieces = _BLANK_LINE_RE.split(text)
    if len(pieces) < 3:
        return text

    result = pieces[0]
    previous = _normalize_duplicate_block(pieces[0])
    i = 1
    while i < len(pieces):
        separator = pieces[i]
        block = pieces[i + 1] if i + 1 < len(pieces) else ""
        normalized = _normalize_duplicate_block(block)
        if (
            normalized
            and normalized == previous
            and len(normalized) >= _DUPLICATE_BLOCK_MIN_CHARS
        ):
            i += 2
            continue
        result += separator + block
        if normalized:
            previous = normalized
        i += 2
    return result


class MysticAgent(Agent):
    """Agent subclass that injects the current wall-clock time before each LLM turn."""

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            tz = ZoneInfo(get_agent_config().hours.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        stamp = now.strftime("Current time: %A, %B %d, %Y %I:%M %p ") + str(tz)
        turn_ctx.items[:] = [
            item for item in turn_ctx.items
            if not (getattr(item, "id", "") or "").startswith(_TIME_MARKER_PREFIX)
        ]
        turn_ctx.add_message(
            role="system",
            content=stamp,
            id=f"{_TIME_MARKER_PREFIX}{int(now.timestamp())}",
        )

    async def transcription_node(
        self,
        text: AsyncIterable[str],
        model_settings: Any,
    ) -> AsyncIterable[str]:
        if not _should_buffer_dashboard_text_output(self):
            async for delta in Agent.default.transcription_node(self, text, model_settings):
                yield delta
            return

        chunks: list[str] = []
        async for delta in Agent.default.transcription_node(self, text, model_settings):
            chunks.append(str(delta))
        collapsed = collapse_adjacent_repeated_text("".join(chunks))
        if collapsed:
            yield collapsed


def _should_buffer_dashboard_text_output(agent: Agent) -> bool:
    try:
        activity = agent._get_activity_or_raise()
    except Exception:
        return False
    session = getattr(activity, "session", None)
    output = getattr(session, "output", None)
    if getattr(output, "audio_enabled", True) is not False:
        return False
    userdata = getattr(session, "userdata", None)
    skill_context = getattr(userdata, "skill_context", None)
    return getattr(skill_context, "channel", None) == "dashboard"


async def create_pipeline(
    config: PipelineConfig,
    system_prompt: str,
    voice_id: str | None,
    db: sqlite3.Connection,
    skill_ctx: SkillContext,
    *,
    agent_cls: Any = MysticAgent,
    stt_factory: Any | None = None,
    tts_factory: Any | None = None,
    llm_factory: Any | None = None,
    vad_loader: Any | None = None,
    include_tools: bool = True,
    extra_tools: list[Any] | None = None,
) -> Agent:
    resolved_voice = voice_id or DEFAULT_VOICE
    tools: list[Any] = build_agent_tools(db, skill_ctx) if include_tools else []
    if extra_tools:
        tools.extend(extra_tools)
    stt_instance = await (stt_factory or create_stt)(config.stt)
    tts_instance = await (tts_factory or create_tts)(config.tts, resolved_voice)
    vad_instance = await _load_vad(vad_loader)
    llm_instance = (llm_factory or create_llm)(config.llm)

    instructions = system_prompt + _TOOL_NARRATION if include_tools else system_prompt
    agent = agent_cls(
        instructions=instructions,
        tools=tools,
        stt=stt_instance,
        tts=tts_instance,
        llm=llm_instance,
        vad=vad_instance,
    )

    logger.info(
        "agent.created",
        audience=skill_ctx.audience,
        direction=skill_ctx.direction,
        callId=skill_ctx.call_id,
    )
    logger.info(
        "agent.stt.config",
        callId=skill_ctx.call_id,
        provider=getattr(config.stt, "provider", "unknown"),
        model=getattr(config.stt, "model", None),
    )
    logger.info(
        "agent.tts.config",
        callId=skill_ctx.call_id,
        provider=getattr(config.tts, "provider", "unknown"),
        model=getattr(config.tts, "model", None),
        voice=resolved_voice,
    )

    return agent


async def create_stt(config: SttConfig) -> agents_stt.STT:
    if isinstance(config, UnconfiguredSttConfig) or not getattr(config, "provider", "").strip():
        raise RuntimeError(
            "STT provider is not configured. Choose one in Dashboard Settings and click Prepare."
        )
    if isinstance(config, DeepgramSttConfig):
        try:
            deepgram = importlib.import_module("livekit.plugins.deepgram")
        except ModuleNotFoundError as exc:
            if exc.name != "livekit.plugins.deepgram":
                raise
            raise RuntimeError(
                "Deepgram STT requires the 'livekit-plugins-deepgram' package. "
                "Install with: pip install livekit-plugins-deepgram"
            ) from exc

        return deepgram.STT(
            model=config.model or "nova-3",
            api_key=config.apiKey,
        )

    moonshine = MoonshineSTT(model=config.model)
    await moonshine.ensure_loaded()
    return moonshine


async def create_tts(config: TtsConfig, voice_id: str | None) -> agents_tts.TTS:
    if isinstance(config, UnconfiguredTtsConfig) or not getattr(config, "provider", "").strip():
        raise RuntimeError(
            "TTS provider is not configured. Choose one in Dashboard Settings and click Prepare."
        )
    if isinstance(config, InworldTtsConfig):
        try:
            inworld = importlib.import_module("livekit.plugins.inworld")
        except ModuleNotFoundError as exc:
            if exc.name != "livekit.plugins.inworld":
                raise
            raise RuntimeError(
                "Inworld TTS requires the 'livekit-plugins-inworld' package. "
                "Install with: pip install livekit-plugins-inworld"
            ) from exc

        return _ensure_streaming_tts(inworld.TTS(
            voice=voice_id or DEFAULT_VOICE,
            model=config.model or "inworld-tts-1.5-mini",
            encoding="LINEAR16",
            sample_rate=TTS_SAMPLE_RATE,
            api_key=config.apiKey,
        ))
    return _ensure_streaming_tts(PocketTTS(config, voice_id=voice_id))


def _ensure_streaming_tts(tts: agents_tts.TTS) -> agents_tts.TTS:
    if type(tts).stream is not agents_tts.TTS.stream:
        return tts
    return agents_tts.StreamAdapter(tts=tts)


def create_llm(config: ResolvedLLMConfig) -> openai.LLM:
    return openai.LLM(
        base_url=config.baseURL,
        api_key=config.apiKey or "not-needed",
        model=config.model,
        max_completion_tokens=DEFAULT_LLM_MAX_COMPLETION_TOKENS,
    )


async def _load_vad(vad_loader: Any | None) -> object:
    if vad_loader is not None:
        loaded = vad_loader()
        if asyncio.iscoroutine(loaded):
            return await loaded
        return loaded

    global _VAD_INSTANCE
    if _VAD_INSTANCE is not None:
        return _VAD_INSTANCE

    async with _VAD_LOCK:
        if _VAD_INSTANCE is None:
            _VAD_INSTANCE = await asyncio.to_thread(
                silero.VAD.load, min_silence_duration=0.3
            )
    return _VAD_INSTANCE


class MoonshineSTT(agents_stt.STT):
    def __init__(self, *, model: str = "small") -> None:
        super().__init__(
            capabilities=agents_stt.STTCapabilities(
                streaming=True,
                interim_results=True,
            )
        )
        self._model = model
        self._transcriber: Any | None = None
        self._transcriber_lock = threading.Lock()

    @property
    def model(self) -> str: return self._model

    @property
    def provider(self) -> str: return "Moonshine"

    async def ensure_loaded(self) -> None:
        await asyncio.to_thread(self._load_transcriber)

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> agents_stt.SpeechEvent:
        del language, conn_options
        await self.ensure_loaded()
        combined = rtc.combine_audio_frames(buffer)
        text = await asyncio.to_thread(self._transcribe_frame, combined)
        duration = combined.duration
        return agents_stt.SpeechEvent(
            type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=shortuuid(),
            alternatives=[
                agents_stt.SpeechData(
                    language=LanguageCode("en"),
                    text=text,
                    start_time=0.0,
                    end_time=duration,
                    confidence=0.9,
                )
            ],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "MoonshineRecognizeStream":
        del language
        return MoonshineRecognizeStream(stt=self, conn_options=conn_options)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_transcriber)

    def _load_transcriber(self) -> None:
        if self._transcriber is not None:
            return

        with self._transcriber_lock:
            if self._transcriber is not None:
                return

            moonshine_voice = _import_moonshine_voice()
            model_arch = _moonshine_model_arch(moonshine_voice.ModelArch, self._model)
            model_path, resolved_arch = moonshine_voice.get_model_for_language("en", model_arch)
            self._transcriber = moonshine_voice.Transcriber(
                model_path=model_path,
                model_arch=resolved_arch,
            )
            logger.info(
                "moonshine.loaded",
                model=self._model,
                modelPath=model_path,
                modelArch=getattr(resolved_arch, "name", str(resolved_arch)),
            )

    def _transcribe_frame(self, frame: rtc.AudioFrame) -> str:
        transcriber = self._require_transcriber()
        samples = _audio_frame_to_numpy(frame)

        with transcriber.create_stream() as stream:
            stream.start()
            stream.add_audio(samples, frame.sample_rate)
            transcript = stream.stop()
        return _extract_moonshine_voice_text(transcript)

    def _require_transcriber(self) -> Any:
        if self._transcriber is None:
            raise RuntimeError("Moonshine STT is not loaded")
        return self._transcriber

    def _close_transcriber(self) -> None:
        transcriber = self._transcriber
        self._transcriber = None
        if transcriber is not None:
            transcriber.close()


class MoonshineRecognizeStream(agents_stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: MoonshineSTT,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=MOONSHINE_SAMPLE_RATE)
        self._moonshine_stt = stt

    async def _run(self) -> None:
        await self._moonshine_stt.ensure_loaded()
        moonshine_voice = _import_moonshine_voice()
        recognize_stream = self
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[agents_stt.SpeechEvent | None] = asyncio.Queue()
        stream_error: Exception | None = None

        def _emit(event: agents_stt.SpeechEvent | None) -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, event)

        async def _drain_events() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    return
                self._event_ch.send_nowait(event)

        class _Listener(moonshine_voice.TranscriptEventListener):
            def on_line_started(self, event: Any) -> None:
                _emit(
                    agents_stt.SpeechEvent(
                        type=agents_stt.SpeechEventType.START_OF_SPEECH,
                        request_id=_moonshine_request_id(event.line),
                    )
                )

            def on_line_text_changed(self, event: Any) -> None:
                speech_data = _moonshine_speech_data(
                    event.line,
                    start_time_offset=recognize_stream.start_time_offset,
                )
                if not speech_data.text:
                    return
                _emit(
                    agents_stt.SpeechEvent(
                        type=agents_stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        request_id=_moonshine_request_id(event.line),
                        alternatives=[speech_data],
                    )
                )

            def on_line_completed(self, event: Any) -> None:
                speech_data = _moonshine_speech_data(
                    event.line,
                    start_time_offset=recognize_stream.start_time_offset,
                )
                if not speech_data.text:
                    return
                _emit(
                    agents_stt.SpeechEvent(
                        type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=_moonshine_request_id(event.line),
                        alternatives=[speech_data],
                    )
                )

            def on_error(self, event: Any) -> None:
                nonlocal stream_error
                stream_error = event.error

        drain_task = asyncio.create_task(_drain_events())
        try:
            with self._moonshine_stt._require_transcriber().create_stream() as stream:
                stream.add_listener(_Listener())
                await asyncio.to_thread(stream.start)

                async for item in self._input_ch:
                    if isinstance(item, self._FlushSentinel):
                        continue
                    await asyncio.to_thread(stream.add_audio, _audio_frame_to_numpy(item), item.sample_rate)

                await asyncio.to_thread(stream.stop)
        except Exception as exc:
            logger.warn("moonshine.stream.error", error=get_error_message(exc))
            raise
        finally:
            _emit(None)
            await drain_task

        if stream_error is not None:
            raise stream_error


def _import_moonshine_voice() -> Any:
    try:
        return importlib.import_module("moonshine_voice")
    except ModuleNotFoundError as exc:
        if exc.name != "moonshine_voice":
            raise
        raise RuntimeError(
            "Moonshine STT requires the 'moonshine-voice' package. "
            "Install with: pip install moonshine-voice"
        ) from exc


def _moonshine_model_arch(model_arch_enum: Any, model: str) -> Any:
    arch_names = {
        "tiny": "TINY_STREAMING",
        "small": "SMALL_STREAMING",
        "medium": "MEDIUM_STREAMING",
    }
    normalized = model.strip().lower() or "small"
    try:
        arch_name = arch_names[normalized]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported Moonshine model: {normalized}") from exc
    return getattr(model_arch_enum, arch_name)


def _moonshine_request_id(line: Any) -> str:
    line_id = getattr(line, "line_id", None)
    if line_id is None:
        return shortuuid()
    return f"moonshine-{line_id}"


def _moonshine_speech_data(line: Any, *, start_time_offset: float = 0.0) -> agents_stt.SpeechData:
    start_time = start_time_offset + float(getattr(line, "start_time", 0.0) or 0.0)
    duration = float(getattr(line, "duration", 0.0) or 0.0)
    text = str(getattr(line, "text", "") or "").strip()
    return agents_stt.SpeechData(
        language=LanguageCode("en"),
        text=text,
        start_time=start_time,
        end_time=start_time + max(duration, 0.0),
        confidence=0.9,
    )


def _extract_moonshine_voice_text(transcript: Any) -> str:
    lines = getattr(transcript, "lines", None)
    if not lines:
        return ""
    parts = [str(getattr(line, "text", "") or "").strip() for line in lines]
    return " ".join(part for part in parts if part).strip()


def _audio_frame_to_numpy(frame: rtc.AudioFrame) -> Any:
    import numpy as np

    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1)
    else:
        pcm = pcm.astype(np.float32, copy=False)
    return np.clip(pcm / 32768.0, -1.0, 1.0).astype(np.float32, copy=False)


class PocketTTS(agents_tts.TTS):
    def __init__(
        self,
        config: PocketTtsConfig,
        *,
        voice_id: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=agents_tts.TTSCapabilities(streaming=True),
            sample_rate=TTS_SAMPLE_RATE,
            num_channels=POCKET_CHANNELS,
        )
        self._config = config
        self._voice_id = _resolve_pocket_voice(voice_id)

    @property
    def model(self) -> str: return self._config.model or "default"

    @property
    def provider(self) -> str: return "PocketTTS"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "PocketChunkedStream":
        return PocketChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None: return None


class PocketChunkedStream(agents_tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: PocketTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: agents_tts.AudioEmitter) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        stop_event = threading.Event()
        sample_rate = TTS_SAMPLE_RATE

        def _queue_item(item: bytes | None) -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, item)

        def _consume_thread_result(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except BaseException:
                return

        def _stream_blocking() -> None:
            nonlocal sample_rate
            try:
                state = _get_pocket_engine(self._tts._config.model)
                engine = state["engine"]
                sample_rate = int(getattr(engine, "SAMPLE_RATE", TTS_SAMPLE_RATE))

                for chunk in engine.stream(self.input_text, voice=self._tts._voice_id):
                    if stop_event.is_set():
                        break
                    _queue_item(_to_pcm16_bytes(chunk))
            finally:
                _queue_item(None)

        thread_task = asyncio.create_task(asyncio.to_thread(_stream_blocking))
        thread_task.add_done_callback(_consume_thread_result)

        initialized = False
        flushed_first_chunk = False

        try:
            while True:
                audio_bytes = await queue.get()
                if audio_bytes is None:
                    break

                if not initialized:
                    output_emitter.initialize(
                        request_id=shortuuid(),
                        sample_rate=sample_rate,
                        num_channels=POCKET_CHANNELS,
                        mime_type="audio/pcm",
                    )
                    initialized = True

                output_emitter.push(audio_bytes)
                if not flushed_first_chunk:
                    output_emitter.flush()
                    flushed_first_chunk = True

            await thread_task
        except asyncio.CancelledError:
            stop_event.set()
            raise

        output_emitter.flush()


def _resolve_pocket_voice(voice_id: str | None) -> str:
    normalized = (voice_id or "").strip()
    if not normalized or normalized.lower() == "default":
        normalized = DEFAULT_VOICE

    voices_dir = Path(__file__).resolve().parents[1] / "voices"
    candidate = voices_dir / f"{normalized.lower()}.wav"
    if candidate.exists():
        return str(candidate)
    return normalized


def pocket_onnx_models_missing() -> list[str]:
    model_root = _get_pocket_onnx_root()
    return [path for path in POCKET_ONNX_FILES if not (model_root / path).exists()]


def ensure_pocket_onnx_models(
    progress_callback_factory: Callable[[str], Callable[[int, int | None], None] | None] | None = None,
) -> Path:
    model_root = _get_pocket_onnx_root()
    model_root.mkdir(parents=True, exist_ok=True)

    for relative_path in POCKET_ONNX_FILES:
        destination = model_root / relative_path
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{POCKET_ONNX_BASE}/{relative_path}"
        progress_callback = progress_callback_factory(relative_path) if progress_callback_factory else None
        _download_pocket_onnx_file(
            url,
            destination,
            relative_path=relative_path,
            progress_callback=progress_callback,
        )

    return model_root


def _get_pocket_onnx_root() -> Path: return get_shared_home() / "models" / "pocket-tts-onnx"


def _download_pocket_onnx_file(
    url: str,
    destination: Path,
    *,
    relative_path: str,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> None:
    temp_path: Path | None = None
    start = time.monotonic()
    try:
        logger.info("pocket.onnx.download.start", file=relative_path, url=url)
        with urllib_request.urlopen(url, timeout=120) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")

            total_size = _response_content_length(response)
            bytes_read = 0
            next_log_threshold = 25 * 1024 * 1024
            if total_size is not None and total_size > 0:
                next_log_threshold = max(total_size // 10, 25 * 1024 * 1024)

            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_read += len(chunk)
                    if progress_callback is not None:
                        progress_callback(bytes_read, total_size)
                    if bytes_read >= next_log_threshold:
                        logger.info(
                            "pocket.onnx.download.progress",
                            file=relative_path,
                            downloadedBytes=bytes_read,
                            totalBytes=total_size,
                            percent=_download_percent(bytes_read, total_size),
                        )
                        if total_size is not None and total_size > 0:
                            next_log_threshold += max(total_size // 10, 25 * 1024 * 1024)
                        else:
                            next_log_threshold += 25 * 1024 * 1024

        assert temp_path is not None
        temp_path.replace(destination)
        logger.info(
            "pocket.onnx.download.finish",
            file=relative_path,
            path=str(destination),
            downloadedBytes=destination.stat().st_size,
            durationMs=round((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        detail = get_error_message(exc)
        raise RuntimeError(f"Failed to download Pocket ONNX file {relative_path}: {detail}") from exc


def _response_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    raw = None if headers is None else headers.get("Content-Length")
    if not raw: return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _download_percent(downloaded: int, total: int | None) -> int | None:
    if total is None or total <= 0: return None
    return int((downloaded / total) * 100)


def _get_pocket_engine(model_name: str | None) -> dict[str, Any]:
    normalized_model = (model_name or "default").strip() or "default"
    cached = _POCKET_ENGINE_CACHE.get(normalized_model)
    if cached is not None:
        return cached

    with _POCKET_ENGINE_LOCK:
        cached = _POCKET_ENGINE_CACHE.get(normalized_model)
        if cached is not None:
            return cached
        loaded = _load_pocket_engine(normalized_model)
        _POCKET_ENGINE_CACHE[normalized_model] = loaded
        return loaded


def _load_pocket_engine(model_name: str) -> dict[str, Any]:
    normalized = (model_name or "default").strip()
    if normalized not in {"", "default"}:
        raise RuntimeError(
            "Pocket ONNX only supports the bundled default model set; "
            f"unsupported tts.model={normalized!r}"
        )

    missing = pocket_onnx_models_missing()
    if missing:
        raise RuntimeError(
            "Pocket ONNX models are missing: "
            + ", ".join(missing)
            + ". Run 'mystic-horizon --agent <name> init' to download them before starting a session."
        )

    model_dir = _get_pocket_onnx_root()
    pocket_tts_class = _import_pocket_tts_onnx_class()
    engine = pocket_tts_class(
        models_dir=str(model_dir / "onnx"),
        tokenizer_path=str(model_dir / "tokenizer.model"),
        precision="fp32",
    )
    return {"engine": engine}


def _import_pocket_tts_onnx_class() -> Any:
    try:
        module = importlib.import_module("pocket_tts_onnx")
    except ModuleNotFoundError as exc:
        if exc.name != "pocket_tts_onnx":
            raise
        vendor_root = Path(__file__).resolve().parents[1] / "vendor"
        if str(vendor_root) not in sys.path:
            sys.path.insert(0, str(vendor_root))
        module = importlib.import_module("pocket_tts_onnx")
    return getattr(module, "PocketTTSOnnx")


def _to_pcm16_bytes(audio: Any) -> bytes:
    import numpy as np

    array = np.asarray(audio).reshape(-1)
    if np.issubdtype(array.dtype, np.floating):
        clipped = np.clip(array.astype(np.float32), -1.0, 1.0)
        pcm = np.rint(clipped * 32767.0).astype(np.int16)
    else:
        clipped = np.clip(array, -32768, 32767)
        pcm = clipped.astype(np.int16)
    return pcm.tobytes()


@dataclass(slots=True)
class AgentToolUserData:
    db: sqlite3.Connection
    skill_context: SkillContext
    room: rtc.Room | None = None
    audio_source: object | None = None
    on_send_text: Callable[[str], Awaitable[None]] | None = None
    on_tool_event: Callable[..., Awaitable[None]] | None = None
    # Optional ship-state cache used only in game rooms — read by the
    # `read_ship_status` tool so the Copilot can query telemetry on demand.
    game_state: object | None = None


def build_agent_tools(
    db: sqlite3.Connection,
    ctx: SkillContext,
    modality: Modality | None = None,
) -> list[Any]:
    try:
        registry = get_registry()
    except RuntimeError:
        registry = init_skills()

    tools: list[Any] = []
    seen_tool_names: set[str] = set()
    for resolved_modality in _tool_modalities_for_context(ctx, modality):
        for tool_schema in build_tools_for_context(registry, ctx.audience, resolved_modality):
            function_schema = tool_schema.get("function")
            if not isinstance(function_schema, dict):
                continue
            tool_name = function_schema.get("name")
            if not isinstance(tool_name, str) or not tool_name or tool_name in seen_tool_names:
                continue
            seen_tool_names.add(tool_name)
            tools.append(
                _make_skill_voice_tool(
                    db,
                    ctx,
                    tool_name=tool_name,
                    function_schema=function_schema,
                    parameter_keys=_schema_parameter_keys(function_schema.get("parameters")),
                )
            )

    return tools


def _tool_modalities_for_context(
    ctx: SkillContext,
    explicit_modality: Modality | None,
) -> tuple[Modality, ...]:
    if explicit_modality is not None:
        return (explicit_modality,)
    if ctx.channel == "dashboard" and ctx.audience == "owner":
        return ("voice", "text")
    if ctx.modality in {"voice", "text"}:
        return (ctx.modality,)
    return ("voice", "text")


def _make_skill_voice_tool(
    db: sqlite3.Connection,
    ctx: SkillContext,
    *,
    tool_name: str,
    function_schema: dict[str, object],
    parameter_keys: tuple[str, ...],
) -> Any:
    @function_tool(raw_schema=function_schema)
    async def _tool(
        run_ctx: RunContext[AgentToolUserData],
        raw_arguments: dict[str, object] | None = None,
    ) -> str:
        arguments = _normalize_raw_tool_arguments(raw_arguments, keys=parameter_keys)
        tool_db, tool_ctx, audio_source = _resolve_runtime_context(run_ctx, db, ctx)
        summary = _voice_tool_args_summary(arguments)
        await _emit_tool_event(run_ctx, "tool_started", tool_name, args_summary=summary)
        t0 = time.monotonic()
        error = False
        try:
            if tool_name == "chat":
                return await _send_chat_tool_message(run_ctx, arguments)
            return await _skills_execute_tool(
                tool_db,
                tool_ctx,
                tool_name,
                arguments,
                audio_source=audio_source,
            )
        except Exception:
            error = True
            raise
        finally:
            elapsed = int((time.monotonic() - t0) * 1000)
            await _emit_tool_event(
                run_ctx,
                "tool_completed",
                tool_name,
                duration_ms=elapsed,
                error=error,
            )

    return _tool


async def _send_chat_tool_message(
    run_ctx: RunContext[AgentToolUserData] | object | None,
    arguments: dict[str, object],
) -> str:
    raw_message = arguments.get("message")
    message = raw_message.strip() if isinstance(raw_message, str) else ""
    if not message:
        return "no message"
    userdata = getattr(run_ctx, "userdata", None)
    on_send = getattr(userdata, "on_send_text", None)
    if on_send is None:
        return "chat unavailable"
    await on_send(message)
    return "sent"


def _schema_parameter_keys(parameters: object) -> tuple[str, ...]:
    if not isinstance(parameters, dict):
        return ()
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(key for key in properties if isinstance(key, str))


def _resolve_runtime_context(
    run_ctx: RunContext[AgentToolUserData] | object | None,
    fallback_db: sqlite3.Connection,
    fallback_ctx: SkillContext,
) -> tuple[sqlite3.Connection, SkillContext, object | None]:
    userdata = getattr(run_ctx, "userdata", None)
    tool_db = getattr(userdata, "db", fallback_db)
    tool_ctx = getattr(userdata, "skill_context", fallback_ctx)
    audio_source = getattr(userdata, "audio_source", None)
    return tool_db, tool_ctx, audio_source


async def _emit_tool_event(
    run_ctx: RunContext[AgentToolUserData] | object | None,
    event_type: str,
    tool_name: str,
    **extra: object,
) -> None:
    userdata = getattr(run_ctx, "userdata", None)
    callback = getattr(userdata, "on_tool_event", None)
    if callback is not None:
        await callback(event_type, tool_name, **extra)


def _voice_tool_args_summary(args: dict[str, object]) -> str:
    """One-line human-readable summary of tool arguments for voice events."""
    query = str(
        args.get("query", "")
        or args.get("text", "")
        or args.get("message", "")
        or args.get("context", "")
        or args.get("content", "")
        or args.get("intent", "")
        or args.get("phone_number", "")
        or args.get("area_code", "")
        or args.get("file", "")
        or args.get("id", "")
    )
    parts: list[str] = []
    if query:
        q = query[:60] + ("\u2026" if len(query) > 60 else "")
        parts.append(q)
    return " \u00b7 ".join(parts) if parts else ""


def _normalize_raw_tool_arguments(
    raw_arguments: dict[str, object] | None,
    *,
    keys: tuple[str, ...],
) -> dict[str, Any]:
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    return {key: arguments.get(key) for key in keys}


_WORKER_EXPORTS = {
    "DEFAULT_MAX_ACTIVE_JOBS", "DEFAULT_VOICE_ENV", "FIRST_MESSAGE_TIMEOUT_SECONDS",
    "MAX_ACTIVE_JOBS_ENV", "TRANSCRIPT_PERSIST_DELAY_SECONDS", "WORKER_TYPE_ENV",
    "RoomMetadata", "WorkerConfig", "WorkerServerType", "_accept_job_request",
    "_agent_entrypoint", "_float_or_none", "_log_worker_task_result", "_string_or_none",
    "compute_worker_load", "parse_room_metadata", "resolve_call_id",
    "resolve_effective_max_active_jobs", "resolve_max_active_jobs",
    "resolve_worker_server_type", "start_agent_worker", "stop_agent_worker",
}


def __getattr__(name: str) -> Any:
    if name in _WORKER_EXPORTS:
        from mystic import worker as _worker
        return getattr(_worker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _WORKER_EXPORTS)
