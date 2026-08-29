"""Worker lifecycle and LiveKit agent entrypoint."""

from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from livekit.agents import (
    AgentServer,
    AgentSession,
    CloseEvent,
    ConversationItemAddedEvent,
    JobContext,
    JobRequest,
    TurnHandlingOptions,
    UserInputTranscribedEvent,
    WorkerOptions,
    WorkerType,
    llm,
)

from mystic.audio import ATTENTION_CUES
from mystic.config import (
    InworldTtsConfig,
    LiveKitConfig,
    PocketTtsConfig,
    TtsConfig,
    bind_trace_id,
    get_error_message,
    get_realtime_llm_config,
    get_stt_config,
    get_tts_config,
    logger,
)
from mystic.db import (
    append_call_transcript,
    close_database,
    get_call_by_id,
    open_database,
)
from mystic.game import (
    ALARM_EVENTS,
    GAME_SYSTEM_PROMPT,
    GAME_VOICE_ID,
    TOPIC_GAME,
    GameState,
    apply_game_tick,
    build_game_tools,
    format_game_event_cue,
    parse_game_packet,
)
from mystic.latency import publish_provider_latency_loop
from mystic.skills import init_skills
from mystic.types import Audience, Channel, Direction, InteractionModality, SkillContext
from mystic.voice import (
    AgentToolUserData,
    DEFAULT_VOICE,
    PipelineConfig,
    TranscriptCollector,
    create_pipeline,
    create_transcript_collector,
)

DEFAULT_MAX_ACTIVE_JOBS = 10
MAX_ACTIVE_JOBS_ENV = "MYSTIC_HORIZON_MAX_ACTIVE_JOBS"
DEFAULT_VOICE_ENV = "MYSTIC_HORIZON_DEFAULT_VOICE_ID"
WORKER_TYPE_ENV = "MYSTIC_HORIZON_WORKER_TYPE"
TRANSCRIPT_PERSIST_DELAY_SECONDS = 0.75
FIRST_MESSAGE_TIMEOUT_SECONDS = 12.0
IDLE_SESSION_TIMEOUT_SECONDS = 120
WORKER_REGISTRATION_TIMEOUT_SECONDS = 15.0
TOPIC_AGENT_EVENTS = "lk.agent.events"
TOPIC_CHAT_INPUT = "lk.chat"
TOPIC_CHAT_DATA = "mh.chat"
TOPIC_VOICE_CONTROL = "mh.voice_control"

_agent_server: AgentServer | None = None
_agent_task: asyncio.Task[None] | None = None


async def _wait_for_worker_registration(
    server: AgentServer,
    task: asyncio.Task[None],
    registered: asyncio.Event,
    *,
    timeout: float | None = None,
) -> None:
    effective_timeout = timeout if timeout is not None else WORKER_REGISTRATION_TIMEOUT_SECONDS
    wait_task = asyncio.create_task(
        registered.wait(),
        name="mystic-agent-worker-registration",
    )
    try:
        done, _pending = await asyncio.wait(
            {wait_task, task},
            timeout=effective_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            await wait_task
            return
        if task in done:
            task.result()
            raise RuntimeError("LiveKit agent worker stopped before registering")
        raise TimeoutError(
            f"LiveKit agent worker did not register within {effective_timeout:.1f}s"
        )
    finally:
        if not wait_task.done():
            wait_task.cancel()
            with suppress(asyncio.CancelledError):
                await wait_task


@dataclass(slots=True, frozen=True)
class WorkerConfig:
    livekit_config: LiveKitConfig
    tts_config: TtsConfig
    default_voice_id: str
    max_active_jobs: int | None = None


@dataclass(slots=True, frozen=True)
class RoomMetadata:
    call_id: str | None = None
    person_id: str | None = None
    audience: Audience | None = None
    direction: Direction | None = None
    channel: Channel | None = None
    modality: InteractionModality | None = None
    system_prompt: str | None = None
    voice_id: str | None = None
    first_message: str | None = None
    bootstrap: bool = False
    attention_cue: bool = False
    no_response_timeout: float | None = None
    chat_call_id: str | None = None
    kind: Literal["dashboard", "game"] = "dashboard"


@dataclass(slots=True, frozen=True)
class WorkerServerType:
    name: Literal["room", "publisher"]
    worker_type: WorkerType


def compute_worker_load(
    active_jobs: int,
    max_active_jobs: int = DEFAULT_MAX_ACTIVE_JOBS,
) -> float:
    if max_active_jobs <= 0:
        return 1.0
    return 1.0 if active_jobs >= max_active_jobs else 0.0


def resolve_max_active_jobs(
    *,
    env_raw: str | None = None,
    configured: int | None = None,
) -> int:
    resolved_env = env_raw if env_raw is not None else os.environ.get(MAX_ACTIVE_JOBS_ENV)
    if resolved_env is not None and resolved_env.strip():
        try:
            parsed = int(resolved_env)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
        logger.warn(
            "agent.worker.max-active-jobs.invalid-env",
            value=resolved_env,
            fallback=configured or DEFAULT_MAX_ACTIVE_JOBS,
            env=MAX_ACTIVE_JOBS_ENV,
        )

    if configured is not None:
        if configured > 0:
            return configured
        logger.warn(
            "agent.worker.max-active-jobs.invalid-config",
            value=configured,
            fallback=DEFAULT_MAX_ACTIVE_JOBS,
        )

    return DEFAULT_MAX_ACTIVE_JOBS


def resolve_effective_max_active_jobs(
    tts_config: TtsConfig,
    *,
    env_raw: str | None = None,
    configured: int | None = None,
) -> int:
    if isinstance(tts_config, PocketTtsConfig):
        return 1
    if isinstance(tts_config, InworldTtsConfig):
        return resolve_max_active_jobs(env_raw=env_raw, configured=configured)
    return resolve_max_active_jobs(env_raw=env_raw, configured=configured)


def resolve_worker_server_type(raw: str | None = None) -> WorkerServerType:
    value = (raw if raw is not None else os.environ.get(WORKER_TYPE_ENV, "room")).strip().lower()
    if value in {"publisher", "jt_publisher", "1"}:
        return WorkerServerType(name="publisher", worker_type=WorkerType.PUBLISHER)
    if value not in {"", "room", "jt_room", "0"}:
        logger.warn("agent.worker.server-type.invalid", value=raw, fallback="room")
    return WorkerServerType(name="room", worker_type=WorkerType.ROOM)


def parse_room_metadata(ctx: JobContext | object) -> RoomMetadata:
    room = getattr(getattr(ctx, "job", None), "room", None)
    raw = getattr(room, "metadata", None)
    if not isinstance(raw, str) or not raw:
        return RoomMetadata()

    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warn(
            "agent.entry.metadata.parse-error",
            room=getattr(room, "name", None),
            error=get_error_message(exc),
        )
        return RoomMetadata()

    if not isinstance(payload, dict):
        return RoomMetadata()

    audience = payload.get("audience")
    direction = payload.get("direction")
    channel = payload.get("channel")
    modality = payload.get("modality")
    kind_raw = payload.get("kind")
    kind: Literal["dashboard", "game"] = "game" if kind_raw == "game" else "dashboard"
    return RoomMetadata(
        call_id=_string_or_none(payload.get("callId")),
        person_id=_string_or_none(payload.get("personId")),
        audience=audience if audience in {"owner", "public"} else None,
        direction=direction if direction in {"inbound", "outbound"} else None,
        channel=channel if channel in {"dashboard", "phone", "sms", "cli"} else None,
        modality=modality if modality in {"voice", "text", "mixed"} else None,
        system_prompt=_string_or_none(payload.get("systemPrompt")),
        voice_id=_string_or_none(payload.get("voiceId")),
        first_message=_string_or_none(payload.get("firstMessage")),
        bootstrap=bool(payload.get("bootstrap")),
        attention_cue=bool(payload.get("attentionCue")),
        no_response_timeout=_float_or_none(payload.get("noResponseTimeout")),
        chat_call_id=_string_or_none(payload.get("chatCallId")),
        kind=kind,
    )


def resolve_call_id(ctx: JobContext | object, metadata: RoomMetadata) -> str | None:
    if metadata.call_id:
        return metadata.call_id

    room = getattr(getattr(ctx, "job", None), "room", None)
    room_name = getattr(room, "name", "")
    if isinstance(room_name, str) and room_name.startswith("call-") and len(room_name) > 5:
        return room_name[5:]
    return None


def _build_turn_handling(*, game_mode: bool = False) -> TurnHandlingOptions:
    fallback: TurnHandlingOptions = {"endpointing": {"min_delay": 0.2}}

    try:
        turn_detector_module = importlib.import_module(
            "livekit.plugins.turn_detector.multilingual"
        )
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "livekit.plugins.turn_detector",
            "livekit.plugins.turn_detector.multilingual",
        }:
            raise
        logger.warn(
            "agent.worker.turn-detector.unavailable",
            hint="pip install livekit-plugins-turn-detector",
        )
        return fallback

    try:
        turn_detector = getattr(turn_detector_module, "MultilingualModel")()
    except Exception as exc:
        logger.warn(
            "agent.worker.turn-detector.init-failed",
            error=get_error_message(exc),
            hint="Using endpointing fallback.",
        )
        return fallback

    logger.info(
        "agent.worker.turn-detector.enabled",
        provider=getattr(turn_detector, "provider", "unknown"),
        model=getattr(turn_detector, "model", "unknown"),
        game_mode=game_mode,
    )
    # Game mode loosens interruption thresholds so short pilot chatter
    # ("shield!", "uh") doesn't hard-cut the Copilot mid-phrase; false
    # interruptions get a longer resume window to stitch speech back.
    # Endpointing stays snappy so tool calls still fire fast.
    if game_mode:
        return {
            "turn_detection": turn_detector,
            "endpointing": {"mode": "dynamic", "min_delay": 0.2, "max_delay": 1.5},
            "interruption": {
                "mode": "vad",
                "resume_false_interruption": True,
                "false_interruption_timeout": 3.5,
                "min_duration": 0.6,
                "min_words": 2,
            },
        }
    return {
        "turn_detection": turn_detector,
        "endpointing": {"mode": "dynamic", "min_delay": 0.2, "max_delay": 1.5},
        "interruption": {
            "mode": "vad",
            "resume_false_interruption": True,
            "false_interruption_timeout": 2.0,
            "min_duration": 0.3,
            "min_words": 1,
        },
    }


async def start_agent_worker(config: WorkerConfig) -> None:
    from mystic.livekit import MYSTIC_HORIZON_AGENT_NAME

    global _agent_server, _agent_task
    if _agent_server is not None:
        return

    requested_max_active_jobs = resolve_max_active_jobs(configured=config.max_active_jobs)
    max_active_jobs = resolve_effective_max_active_jobs(
        config.tts_config,
        configured=config.max_active_jobs,
    )
    if getattr(config.tts_config, "provider", "") == "pocket" and requested_max_active_jobs != 1:
        logger.warn(
            "agent.worker.max-active-jobs.pocket-clamped",
            requested=requested_max_active_jobs,
            effective=max_active_jobs,
        )

    worker_type = resolve_worker_server_type()
    ws_url = f"ws://{config.livekit_config.host}:{config.livekit_config.port}"
    os.environ["LIVEKIT_URL"] = ws_url
    os.environ["LIVEKIT_API_KEY"] = config.livekit_config.apiKey
    os.environ["LIVEKIT_API_SECRET"] = config.livekit_config.apiSecret
    os.environ[DEFAULT_VOICE_ENV] = config.default_voice_id

    options = WorkerOptions(
        entrypoint_fnc=_agent_entrypoint,
        request_fnc=_accept_job_request,
        load_fnc=lambda server: compute_worker_load(len(server.active_jobs), max_active_jobs),
        load_threshold=1.0,
        port=0,
        ws_url=ws_url,
        api_key=config.livekit_config.apiKey,
        api_secret=config.livekit_config.apiSecret,
        agent_name=MYSTIC_HORIZON_AGENT_NAME,
        worker_type=worker_type.worker_type,
    )

    server = AgentServer.from_server_options(options)
    registered = asyncio.Event()

    def _mark_registered(*_args: object) -> None:
        registered.set()

    server.on("worker_registered", _mark_registered)
    task = asyncio.create_task(server.run(), name="mystic-agent-worker")
    task.add_done_callback(_log_worker_task_result)

    _agent_server = server
    _agent_task = task

    try:
        await _wait_for_worker_registration(server, task, registered)
    except BaseException:
        with suppress(Exception):
            server.off("worker_registered", _mark_registered)
        await stop_agent_worker()
        raise
    else:
        with suppress(Exception):
            server.off("worker_registered", _mark_registered)

    logger.info(
        "agent.worker.started",
        url=ws_url,
        agentName=MYSTIC_HORIZON_AGENT_NAME,
        serverType=worker_type.name,
        maxActiveJobs=max_active_jobs,
    )


async def stop_agent_worker() -> None:
    global _agent_server, _agent_task
    if _agent_server is None:
        return

    server = _agent_server
    task = _agent_task
    _agent_server = None
    _agent_task = None

    drained = False
    try:
        await server.drain(timeout=3)
        drained = True
    except Exception as exc:
        logger.warn("agent.worker.drain.timeout", error=get_error_message(exc))
        try:
            await server.drain(timeout=0)
        except Exception:
            pass

    try:
        await asyncio.wait_for(server.aclose(), timeout=3)
    except (TimeoutError, Exception):
        pass
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except (TimeoutError, Exception):
            pass

    logger.info("agent.worker.stopped", drained=drained)


async def _accept_job_request(req: JobRequest) -> None:
    logger.info(
        "agent.worker.job.received",
        jobId=req.id,
        room=getattr(req.room, "name", None),
        agentName=req.agent_name,
    )
    await req.accept()


async def _agent_entrypoint(ctx: JobContext) -> None:
    from mystic.calls import handle_end_of_call_report_by_call_id

    db = open_database()
    session: AgentSession[AgentToolUserData] | None = None
    collector: TranscriptCollector | None = None
    persist_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None
    latency_task: asyncio.Task[None] | None = None
    user_spoke: asyncio.Event | None = None
    close_event = asyncio.Event()
    voice_audio_enabled = True
    call_id: str | None = None
    transcript = ""
    duration_seconds = 0

    async def _shutdown_session(_: str = "") -> None:
        if session is not None:
            await session.aclose()

    ctx.add_shutdown_callback(_shutdown_session)

    try:
        init_skills()

        metadata = parse_room_metadata(ctx)
        if metadata.kind == "game":
            await _game_entrypoint(ctx)
            return

        call_id = resolve_call_id(ctx, metadata)
        if not call_id:
            logger.error("agent.entry.missing-call-id", room=getattr(ctx.job.room, "name", None))
            return
        bind_trace_id(call_id)

        call = get_call_by_id(db, call_id)
        person_id = metadata.person_id or (call.person_id if call is not None else None)
        if not person_id:
            logger.error("agent.entry.missing-person-id", callId=call_id)
            return

        audience: Audience = metadata.audience or (call.audience if call is not None else "public")
        direction: Direction = metadata.direction or (call.direction if call is not None else "inbound")
        channel = metadata.channel or (call.channel if call is not None else None)
        modality = metadata.modality or (call.modality if call is not None else None)
        if channel is None or modality is None:
            logger.error("agent.entry.missing-channel-or-modality", callId=call_id)
            return
        voice_id = metadata.voice_id or os.environ.get(DEFAULT_VOICE_ENV) or DEFAULT_VOICE
        system_prompt = metadata.system_prompt or ""
        chat_call_id = metadata.chat_call_id
        chat_call_started_at: int = 0
        if chat_call_id:
            chat_call = get_call_by_id(db, chat_call_id)
            if chat_call is not None:
                chat_call_started_at = chat_call.started_at

        logger.info(
            "agent.entry.started",
            callId=call_id,
            audience=audience,
            direction=direction,
            channel=channel,
            modality=modality,
            room=getattr(ctx.job.room, "name", None),
        )

        await ctx.connect()
        await ctx.wait_for_participant()

        collector = create_transcript_collector()
        skill_context = SkillContext(
            audience=audience,
            direction=direction,
            channel=channel,
            modality=modality,
            call_id=call_id,
            person_id=person_id,
            source="mid-call",
        )
        try:
            stt_config = get_stt_config()
            tts_config = get_tts_config()
            llm_config = get_realtime_llm_config()
            pipeline = await create_pipeline(
                PipelineConfig(
                    stt=stt_config,
                    tts=tts_config,
                    llm=llm_config,
                ),
                system_prompt,
                voice_id,
                db,
                skill_context,
            )
        except Exception as exc:
            logger.error(
                "agent.entry.pipeline.error",
                callId=call_id,
                error=get_error_message(exc),
            )
            local_participant = getattr(getattr(ctx, "room", None), "local_participant", None)
            if local_participant is not None:
                try:
                    await local_participant.publish_data(
                        json.dumps({"type": "agent_error", "message": f"Failed to start voice pipeline: {get_error_message(exc)}"}).encode("utf-8"),
                        topic=TOPIC_AGENT_EVENTS,
                        reliable=True,
                    )
                except Exception:
                    pass
            return

        session = AgentSession(
            turn_handling=_build_turn_handling(),
            userdata=AgentToolUserData(
                db=db,
                skill_context=skill_context,
                room=ctx.room,
            ),
        )

        # Packets received on the data channel before `session.start()`
        # completes must not be dropped — the browser can publish chat or
        # voice-control packets the moment it joins the LiveKit room,
        # which happens well before the agent's pipeline finishes loading
        # on a cold start.
        session_ready = asyncio.Event()
        early_chat_queue: list[tuple[str, str | None]] = []
        early_voice_queue: list[tuple[str, str | None]] = []

        def persist_snapshot() -> None:
            if collector is None or call_id is None:
                return
            delta = collector.peek_delta_transcript().strip()
            if not delta:
                return
            append_call_transcript(db, call_id, delta)
            collector.consume_delta_transcript()

        def schedule_persist() -> None:
            nonlocal persist_task
            if persist_task is not None and not persist_task.done():
                return

            async def _persist() -> None:
                try:
                    await asyncio.sleep(TRANSCRIPT_PERSIST_DELAY_SECONDS)
                    persist_snapshot()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warn(
                        "agent.entry.transcript.persist.error",
                        callId=call_id,
                        error=get_error_message(exc),
                    )

            persist_task = asyncio.create_task(_persist(), name=f"transcript-persist-{call_id}")

        async def publish_agent_event(
            payload: dict[str, Any],
            *,
            event_type: str,
        ) -> None:
            local_participant = getattr(getattr(ctx, "room", None), "local_participant", None)
            if local_participant is None:
                return
            try:
                await local_participant.publish_data(
                    json.dumps(payload).encode("utf-8"),
                    topic=TOPIC_AGENT_EVENTS,
                    reliable=True,
                )
            except Exception as exc:
                logger.warn(
                    "agent.entry.transcript.publish.error",
                    callId=call_id,
                    eventType=event_type,
                    error=get_error_message(exc),
                )

        if channel == "dashboard":
            async def publish_latency_event(payload: dict[str, Any]) -> None:
                await publish_agent_event(payload, event_type="provider_latency")

            latency_task = _start_provider_latency_task(
                publish_latency_event,
                stt_config,
                llm_config,
                tts_config,
                name=f"provider-latency-{call_id}",
            )

        def _append_chat_entry(speaker: str, text: str) -> None:
            if not chat_call_id:
                return
            content = text.strip()
            if not content:
                return
            elapsed = max(0, (int(time.time() * 1000) - chat_call_started_at) // 1000)
            minutes, seconds = divmod(elapsed, 60)
            label = "Agent" if speaker == "agent" else "Caller"
            entry = f"[{minutes}:{seconds:02d}] {label} [text]: {content}"
            try:
                append_call_transcript(db, chat_call_id, entry)
            except Exception as exc:
                logger.warn(
                    "agent.entry.chat.persist.error",
                    callId=chat_call_id,
                    error=get_error_message(exc),
                )

        def _append_chat_tool_event(event_type: str, tool_name: str, payload: dict[str, object]) -> None:
            if not chat_call_id:
                return
            event_type = event_type.strip()
            tool_name = tool_name.strip() or "tool"
            if event_type not in {"tool_started", "tool_completed"}:
                return

            event: dict[str, object] = {"type": event_type, "name": tool_name}
            for key in ("args_summary", "duration_ms", "error"):
                if key in payload:
                    event[key] = payload[key]

            elapsed = max(0, (int(time.time() * 1000) - chat_call_started_at) // 1000)
            minutes, seconds = divmod(elapsed, 60)
            entry = f"[{minutes}:{seconds:02d}] Tool [event]: {json.dumps(event, separators=(',', ':'))}"
            try:
                append_call_transcript(db, chat_call_id, entry)
            except Exception as exc:
                logger.warn(
                    "agent.entry.chat_tool_event.persist.error",
                    callId=chat_call_id,
                    eventType=event_type,
                    toolName=tool_name,
                    error=get_error_message(exc),
                )

        def _reset_idle_timer() -> None:
            nonlocal idle_task
            if idle_task is not None and not idle_task.done():
                idle_task.cancel()

            async def _idle_watchdog() -> None:
                try:
                    await asyncio.sleep(IDLE_SESSION_TIMEOUT_SECONDS)
                    logger.info("agent.entry.idle.timeout", callId=call_id)
                    if session is not None:
                        await session.aclose()
                except asyncio.CancelledError:
                    pass

            idle_task = asyncio.create_task(_idle_watchdog(), name=f"idle-{call_id}")

        async def _on_send_text(text: str) -> None:
            await publish_agent_event(
                {"type": "agent_chat_response", "text": text},
                event_type="agent_chat_response",
            )
            if collector is not None:
                collector.add_agent_speech(text, modality="text")
                schedule_persist()
            _append_chat_entry("agent", text)

        session.userdata.on_send_text = _on_send_text

        async def _set_session_audio_enabled(enabled: bool) -> None:
            if session is None:
                return
            for attr in ("output", "input"):
                endpoint = getattr(session, attr, None)
                setter = getattr(endpoint, "set_audio_enabled", None)
                if not callable(setter):
                    continue
                result = setter(enabled)
                if inspect.isawaitable(result):
                    await result

        @session.on("user_input_transcribed")
        def _on_user_input_transcribed(event: UserInputTranscribedEvent) -> None:
            transcript = event.transcript.strip()
            if event.is_final and transcript:
                _reset_idle_timer()
                if collector is not None:
                    collector.add_user_speech(event.transcript)
                    schedule_persist()
                if user_spoke is not None:
                    user_spoke.set()
                asyncio.create_task(
                    publish_agent_event(
                        {
                            "type": "user_input_transcribed",
                            "transcript": transcript,
                            "is_final": True,
                        },
                        event_type="user_input_transcribed",
                    )
                )

        @session.on("conversation_item_added")
        def _on_conversation_item_added(event: ConversationItemAddedEvent) -> None:
            if not isinstance(event.item, llm.ChatMessage):
                return
            metrics = getattr(event.item, "metrics", None) or {}
            if event.item.role == "user":
                td = metrics.get("transcription_delay")
                eot = metrics.get("end_of_turn_delay")
                if td is not None or eot is not None:
                    logger.info(
                        "metrics.turn",
                        callId=call_id,
                        transcription_delay=round(td, 3) if td is not None else None,
                        end_of_turn_delay=round(eot, 3) if eot is not None else None,
                    )
                return
            if event.item.role != "assistant":
                return
            text = (event.item.text_content or "").strip()
            if not text:
                return
            ttft = metrics.get("llm_node_ttft")
            ttfb = metrics.get("tts_node_ttfb")
            e2e = metrics.get("e2e_latency")
            if ttft is not None or ttfb is not None or e2e is not None:
                logger.info(
                    "metrics.response",
                    callId=call_id,
                    llm_ttft=round(ttft, 3) if ttft is not None else None,
                    tts_ttfb=round(ttfb, 3) if ttfb is not None else None,
                    e2e_latency=round(e2e, 3) if e2e is not None else None,
                )
            if collector is not None:
                collector.add_agent_speech(
                    text,
                    modality="voice" if voice_audio_enabled else "text",
                )
                schedule_persist()
            event_type: str = (
                "agent_voice_transcribed"
                if voice_audio_enabled
                else "agent_chat_response"
            )
            event_payload: dict[str, Any] = (
                {"type": event_type, "transcript": text, "is_final": True}
                if voice_audio_enabled
                else {"type": event_type, "text": text}
            )
            asyncio.create_task(
                publish_agent_event(
                    event_payload,
                    event_type=event_type,
                )
            )
            if not voice_audio_enabled:
                _append_chat_entry("agent", text)

        @session.once("close")
        def _on_close(_: CloseEvent) -> None:
            close_event.set()

        from livekit.agents import room_io

        seen_text_stream_ids: set[str] = set()

        def _text_stream_id(info: Any | None) -> str | None:
            stream_id = getattr(info, "stream_id", None)
            if isinstance(stream_id, str) and stream_id.strip():
                return stream_id
            return None

        async def _process_text_input(
            sess: AgentSession[AgentToolUserData],
            text: str,
            *,
            source: str,
            info: Any | None = None,
            participant_identity: str | None = None,
            client_message_id: str | None = None,
            skip_ack: bool = False,
        ) -> None:
            text = text.strip()
            if not text:
                return

            stream_id = _text_stream_id(info)
            client_message_id = client_message_id.strip() if client_message_id else None
            dedupe_id = client_message_id or stream_id
            if dedupe_id is not None:
                if dedupe_id in seen_text_stream_ids:
                    logger.info(
                        "agent.entry.text_input.duplicate",
                        callId=call_id,
                        source=source,
                        streamId=stream_id,
                        clientMessageId=client_message_id,
                    )
                    return
                seen_text_stream_ids.add(dedupe_id)

            _reset_idle_timer()
            logger.info(
                "agent.entry.text_input.received",
                callId=call_id,
                source=source,
                participant=participant_identity,
                streamId=stream_id,
                clientMessageId=client_message_id,
                length=len(text),
            )

            # Acknowledge receipt immediately so the browser's pending
            # ack timer resolves before any blocking work (interrupt,
            # LLM call) begins. When draining queued early messages the
            # ack was already published at receive time, so we skip it
            # here to avoid double-ack which would re-trigger the
            # browser's "not resolved" branch and duplicate the message.
            if not skip_ack:
                event_payload: dict[str, Any] = {
                    "type": "user_chat_received",
                    "text": text,
                    "streamId": stream_id,
                }
                if client_message_id is not None:
                    event_payload["clientMessageId"] = client_message_id
                await publish_agent_event(
                    event_payload,
                    event_type="user_chat_received",
                )

            await sess.interrupt()

            if collector is not None:
                collector.add_user_speech(text, modality="text")
                schedule_persist()
            _append_chat_entry("user", text)
            if user_spoke is not None:
                user_spoke.set()

            sess.generate_reply(user_input=text)

        async def _safe_process_text_input(
            sess: AgentSession[AgentToolUserData],
            text: str,
            *,
            source: str,
            info: Any | None = None,
            participant_identity: str | None = None,
            client_message_id: str | None = None,
            skip_ack: bool = False,
        ) -> None:
            try:
                await _process_text_input(
                    sess,
                    text,
                    source=source,
                    info=info,
                    participant_identity=participant_identity,
                    client_message_id=client_message_id,
                    skip_ack=skip_ack,
                )
            except Exception as exc:
                logger.warn(
                    "agent.entry.text_input.error",
                    callId=call_id,
                    source=source,
                    clientMessageId=client_message_id,
                    error=get_error_message(exc),
                )
                await publish_agent_event(
                    {"type": "agent_error", "message": get_error_message(exc)},
                    event_type="agent_error",
                )

        async def _on_text_input(
            sess: AgentSession[AgentToolUserData],
            event: room_io.TextInputEvent,
        ) -> None:
            await _safe_process_text_input(
                sess,
                event.text,
                source="room_io",
                info=event.info,
            )

        text_input = room_io.TextInputOptions(text_input_cb=_on_text_input)
        room_options = room_io.RoomOptions(text_input=text_input)
        is_dashboard_owner_session = (
            direction == "inbound"
            and audience == "owner"
            and bool(metadata.chat_call_id)
        )
        if is_dashboard_owner_session:
            room_options.close_on_disconnect = False
        if modality == "text" and not is_dashboard_owner_session:
            room_options.audio_input = False
            room_options.audio_output = False

        # Register the data-channel handler before starting the session
        # so packets arriving during pipeline warm-up (which is slow on a
        # cold start) are captured. `_handle_voice_control` is defined
        # later; late binding resolves it when the handler fires, which
        # only happens after `session_ready` is set below.
        @ctx.room.on("data_received")
        def _on_data_received(packet: Any) -> None:
            # Any activity on the data channel (including heartbeats and
            # unknown topics) is a liveness signal — reset the idle
            # timer immediately, before dispatching by topic. Pre-ready
            # packets still reset the timer; `_reset_idle_timer` is
            # idempotent and creates the task lazily.
            if session_ready.is_set():
                _reset_idle_timer()
            topic = getattr(packet, "topic", None)
            if topic == TOPIC_VOICE_CONTROL:
                try:
                    payload = json.loads(bytes(packet.data).decode("utf-8"))
                    action = str(payload.get("action", "")).strip().lower()
                except Exception:
                    return
                packet_participant = getattr(packet, "participant", None)
                participant_identity = getattr(packet_participant, "identity", None)
                resolved_identity = (
                    participant_identity.strip()
                    if isinstance(participant_identity, str) and participant_identity.strip()
                    else None
                )
                if not session_ready.is_set():
                    early_voice_queue.append((action, resolved_identity))
                    return
                asyncio.create_task(
                    _handle_voice_control(
                        action,
                        participant_identity=resolved_identity,
                    ),
                    name=f"voice-control-{call_id}",
                )
                return
            if topic != TOPIC_CHAT_DATA:
                return
            try:
                payload = json.loads(bytes(packet.data).decode("utf-8"))
                text = str(payload.get("text", "")).strip()
                raw_client_message_id = payload.get("clientMessageId")
                client_message_id = (
                    raw_client_message_id.strip()
                    if isinstance(raw_client_message_id, str)
                    else None
                )
            except Exception:
                return
            if not text:
                return
            if not session_ready.is_set():
                ack_payload: dict[str, Any] = {
                    "type": "user_chat_received",
                    "text": text,
                }
                if client_message_id is not None:
                    ack_payload["clientMessageId"] = client_message_id
                asyncio.create_task(
                    publish_agent_event(
                        ack_payload,
                        event_type="user_chat_received",
                    ),
                    name=f"early-ack-{call_id}",
                )
                early_chat_queue.append((text, client_message_id))
                return
            asyncio.create_task(
                _safe_process_text_input(
                    session,
                    text,
                    source="data_channel",
                    client_message_id=client_message_id,
                ),
                name=f"text-input-{call_id}",
            )

        try:
            await session.start(pipeline, room=ctx.room, room_options=room_options)
        except TypeError as exc:
            if "room_options" not in str(exc):
                raise
            logger.warn("agent.entry.room_options.unsupported", callId=call_id)
            await session.start(pipeline, room=ctx.room)

        session_output = getattr(session, "output", None)
        session.userdata.audio_source = getattr(session_output, "audio", None)
        if direction == "inbound" and audience == "owner":
            await _set_session_audio_enabled(False)
            voice_audio_enabled = False

        async def _on_tool_event(event_type: str, tool_name: str, **extra: object) -> None:
            if channel == "dashboard" and collector is not None:
                try:
                    collector.add_tool_event(event_type, tool_name, extra)
                    persist_snapshot()
                    _append_chat_tool_event(event_type, tool_name, extra)
                except Exception as exc:
                    logger.warn(
                        "agent.entry.tool_event.persist.error",
                        callId=call_id,
                        eventType=event_type,
                        toolName=tool_name,
                        error=get_error_message(exc),
                    )
            await publish_agent_event(
                {"type": event_type, "name": tool_name, **extra},
                event_type=event_type,
            )

        session.userdata.on_tool_event = _on_tool_event

        async def _handle_voice_control(
            action: str,
            *,
            participant_identity: str | None = None,
        ) -> None:
            nonlocal voice_audio_enabled
            if action == "start":
                room_io_adapter = getattr(session, "room_io", None)
                if participant_identity and room_io_adapter is not None:
                    setter = getattr(room_io_adapter, "set_participant", None)
                    if callable(setter):
                        result = setter(participant_identity)
                        if inspect.isawaitable(result):
                            await result
                # Cycle audio off→on so the session re-subscribes to
                # tracks from the current participant (needed after a
                # browser page refresh reconnects to the same room).
                await _set_session_audio_enabled(False)
                await _set_session_audio_enabled(True)
                voice_audio_enabled = True
            elif action == "stop":
                await _set_session_audio_enabled(False)
                voice_audio_enabled = False
            else:
                return
            logger.info(
                "agent.entry.voice_control",
                callId=call_id,
                action=action,
                audioEnabled=voice_audio_enabled,
                participant=participant_identity,
            )

        session_ready.set()
        drained_chat = len(early_chat_queue)
        drained_voice = len(early_voice_queue)
        if drained_chat or drained_voice:
            logger.info(
                "agent.entry.early_packets.drain",
                callId=call_id,
                chat=drained_chat,
                voice=drained_voice,
            )
        for queued_text, queued_client_message_id in early_chat_queue:
            asyncio.create_task(
                _safe_process_text_input(
                    session,
                    queued_text,
                    source="data_channel_early",
                    client_message_id=queued_client_message_id,
                    skip_ack=True,
                ),
                name=f"text-input-queued-{call_id}",
            )
        early_chat_queue.clear()
        for queued_action, queued_participant in early_voice_queue:
            asyncio.create_task(
                _handle_voice_control(
                    queued_action,
                    participant_identity=queued_participant,
                ),
                name=f"voice-control-queued-{call_id}",
            )
        early_voice_queue.clear()

        _reset_idle_timer()

        if metadata.no_response_timeout:
            user_spoke = asyncio.Event()

            async def _watchdog() -> None:
                try:
                    await asyncio.wait_for(user_spoke.wait(), timeout=metadata.no_response_timeout)
                except TimeoutError:
                    logger.info("agent.entry.watchdog.timeout", callId=call_id)
                    if session is not None:
                        await session.aclose()

            watchdog_task = asyncio.create_task(_watchdog(), name=f"watchdog-{call_id}")

        if metadata.attention_cue:
            import random

            cue = random.choice(ATTENTION_CUES)
            cue_handle = session.say(cue, allow_interruptions=False)
            try:
                await asyncio.wait_for(
                    cue_handle.wait_for_playout(),
                    timeout=FIRST_MESSAGE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warn("agent.entry.attention-cue.timeout", callId=call_id)
            await asyncio.sleep(0.8)

        if metadata.first_message and metadata.first_message.strip():
            speech_handle = session.say(
                metadata.first_message.strip(),
                allow_interruptions=False,
            )
            try:
                await asyncio.wait_for(
                    speech_handle.wait_for_playout(),
                    timeout=FIRST_MESSAGE_TIMEOUT_SECONDS,
                )
                logger.info("agent.entry.first-message.played", callId=call_id)
            except TimeoutError:
                logger.warn("agent.entry.first-message.timeout", callId=call_id)
        elif metadata.bootstrap:
            session.generate_reply()

        await close_event.wait()
        transcript = collector.to_transcript() if collector is not None else ""
        duration_seconds = collector.get_duration() if collector is not None else 0
    except Exception as exc:
        logger.error(
            "agent.entry.error",
            callId=call_id,
            error=get_error_message(exc),
        )
    finally:
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass

        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        if persist_task is not None and not persist_task.done():
            persist_task.cancel()
            try:
                await persist_task
            except asyncio.CancelledError:
                pass

        if latency_task is not None and not latency_task.done():
            latency_task.cancel()
            try:
                await latency_task
            except asyncio.CancelledError:
                pass

        if call_id and collector is not None:
            remaining = collector.peek_delta_transcript().strip()
            if remaining:
                try:
                    append_call_transcript(db, call_id, remaining)
                except Exception as exc:
                    logger.warn(
                        "agent.entry.transcript.persist.error",
                        callId=call_id,
                        error=get_error_message(exc),
                    )

        if session is not None:
            try:
                await session.aclose()
            except Exception:
                pass

        if call_id:
            await handle_end_of_call_report_by_call_id(
                db,
                call_id,
                transcript,
                duration_seconds,
            )

        close_database(db)


async def _game_entrypoint(ctx: JobContext) -> None:
    """Harbormaster/Asteroids session — its own room, its own persona, no call machinery.

    Runs in a short-lived room created by the game token endpoint. The room closes
    when the browser disconnects; we do not persist transcripts, call records, or
    memory. The agent is built with `GAME_SYSTEM_PROMPT` and Hades TTS from birth.
    """
    from livekit.agents import room_io

    room = getattr(ctx.job, "room", None)
    room_name = getattr(room, "name", None) or "game"
    bind_trace_id(room_name)

    db = open_database()
    session: AgentSession[AgentToolUserData] | None = None
    idle_task: asyncio.Task[None] | None = None
    latency_task: asyncio.Task[None] | None = None
    close_event = asyncio.Event()

    async def _shutdown_session(_: str = "") -> None:
        if session is not None:
            await session.aclose()

    ctx.add_shutdown_callback(_shutdown_session)

    try:
        skill_context = SkillContext(
            audience="public",
            direction="inbound",
            channel="dashboard",
            modality="voice",
            call_id=room_name,
            person_id="game",
            source="mid-call",
        )

        logger.info("agent.entry.game.started", room=room_name)
        await ctx.connect()
        await ctx.wait_for_participant()

        try:
            stt_config = get_stt_config()
            tts_config = get_tts_config()
            llm_config = get_realtime_llm_config()
            pipeline = await create_pipeline(
                PipelineConfig(
                    stt=stt_config,
                    tts=tts_config,
                    llm=llm_config,
                ),
                GAME_SYSTEM_PROMPT,
                GAME_VOICE_ID,
                db,
                skill_context,
                include_tools=False,
                extra_tools=build_game_tools(),
            )
        except Exception as exc:
            logger.error(
                "agent.entry.game.pipeline.error",
                room=room_name,
                error=get_error_message(exc),
            )
            return

        # Server-side ship-state cache. The Copilot cannot see the game
        # directly — they query this via `read_ship_status`. Client
        # `game_tick` packets update it silently.
        game_state = GameState()

        session = AgentSession(
            turn_handling=_build_turn_handling(game_mode=True),
            userdata=AgentToolUserData(
                db=db,
                skill_context=skill_context,
                room=ctx.room,
                game_state=game_state,
            ),
        )

        session_ready = asyncio.Event()
        early_chat_queue: list[tuple[str, str | None]] = []
        early_game_queue: list[tuple[str, dict[str, Any]]] = []
        early_voice_queue: list[str] = []
        seen_text_stream_ids: set[str] = set()

        async def publish_agent_event(payload: dict[str, Any]) -> None:
            local_participant = getattr(getattr(ctx, "room", None), "local_participant", None)
            if local_participant is None:
                return
            with suppress(Exception):
                await local_participant.publish_data(
                    json.dumps(payload).encode("utf-8"),
                    topic=TOPIC_AGENT_EVENTS,
                    reliable=True,
                )

        latency_task = _start_provider_latency_task(
            publish_agent_event,
            stt_config,
            llm_config,
            tts_config,
            name=f"provider-latency-{room_name}",
        )

        def _reset_idle_timer() -> None:
            nonlocal idle_task
            if idle_task is not None and not idle_task.done():
                idle_task.cancel()

            async def _idle_watchdog() -> None:
                try:
                    await asyncio.sleep(IDLE_SESSION_TIMEOUT_SECONDS)
                    logger.info("agent.entry.game.idle.timeout", room=room_name)
                    if session is not None:
                        await session.aclose()
                except asyncio.CancelledError:
                    pass

            idle_task = asyncio.create_task(_idle_watchdog(), name=f"game-idle-{room_name}")

        async def _set_audio_enabled(enabled: bool) -> None:
            if session is None:
                return
            for attr in ("output", "input"):
                endpoint = getattr(session, attr, None)
                setter = getattr(endpoint, "set_audio_enabled", None)
                if not callable(setter):
                    continue
                result = setter(enabled)
                if inspect.isawaitable(result):
                    await result

        @session.on("conversation_item_added")
        def _on_item(event: ConversationItemAddedEvent) -> None:
            if not isinstance(event.item, llm.ChatMessage) or event.item.role != "assistant":
                return
            text = (event.item.text_content or "").strip()
            if not text:
                return
            asyncio.create_task(
                publish_agent_event(
                    {"type": "agent_voice_transcribed", "transcript": text, "is_final": True}
                )
            )

        @session.on("user_input_transcribed")
        def _on_user_input(event: UserInputTranscribedEvent) -> None:
            transcript = event.transcript.strip()
            if event.is_final and transcript:
                _reset_idle_timer()
                asyncio.create_task(
                    publish_agent_event(
                        {
                            "type": "user_input_transcribed",
                            "transcript": transcript,
                            "is_final": True,
                        }
                    )
                )

        @session.once("close")
        def _on_close(_: CloseEvent) -> None:
            close_event.set()

        async def _process_chat(text: str, client_message_id: str | None, *, skip_ack: bool) -> None:
            text = text.strip()
            if not text:
                return
            if client_message_id and client_message_id in seen_text_stream_ids:
                return
            if client_message_id:
                seen_text_stream_ids.add(client_message_id)
            _reset_idle_timer()
            if not skip_ack:
                ack: dict[str, Any] = {"type": "user_chat_received", "text": text}
                if client_message_id:
                    ack["clientMessageId"] = client_message_id
                await publish_agent_event(ack)
            with suppress(Exception):
                await session.interrupt()  # type: ignore[union-attr]
            session.generate_reply(user_input=text)  # type: ignore[union-attr]

        async def _process_game_event(event_type: str, payload: dict[str, Any]) -> None:
            if session is None:
                return
            _reset_idle_timer()

            # game_tick: silent state cache update. The Copilot reads state
            # on demand via read_ship_status — no prompt injection.
            if event_type == "game_tick":
                apply_game_tick(game_state, payload)
                return

            # Transitions that mutate ship-system state (cooldowns, etc).
            if event_type == "wave_cleared":
                game_state.bump_cooldown()
            elif event_type == "shield_online":
                game_state.arm_shield()

            # Speak-events get a neutral cue line; the Copilot voices it via
            # its system prompt. Everything else is silent telemetry.
            if event_type not in ALARM_EVENTS:
                return

            cue = format_game_event_cue(event_type, payload)
            if not cue:
                return
            with suppress(Exception):
                await session.interrupt()
            try:
                session.generate_reply(instructions=cue)
            except Exception as exc:
                logger.warn(
                    "agent.entry.game.cue.error",
                    room=room_name,
                    eventType=event_type,
                    error=get_error_message(exc),
                )

        async def _process_voice_control(action: str) -> None:
            if action == "start":
                await _set_audio_enabled(True)
            elif action == "stop":
                await _set_audio_enabled(False)

        @ctx.room.on("data_received")
        def _on_data(packet: Any) -> None:
            if session_ready.is_set():
                _reset_idle_timer()
            topic = getattr(packet, "topic", None)
            if topic == TOPIC_GAME:
                parsed = parse_game_packet(packet.data)
                if parsed is None:
                    return
                event_type, payload = parsed
                if not session_ready.is_set():
                    early_game_queue.append((event_type, payload))
                    return
                asyncio.create_task(
                    _process_game_event(event_type, payload),
                    name=f"game-event-{room_name}",
                )
                return
            if topic == TOPIC_VOICE_CONTROL:
                try:
                    payload = json.loads(bytes(packet.data).decode("utf-8"))
                    action = str(payload.get("action", "")).strip().lower()
                except Exception:
                    return
                if action not in {"start", "stop"}:
                    return
                if not session_ready.is_set():
                    early_voice_queue.append(action)
                    return
                asyncio.create_task(
                    _process_voice_control(action),
                    name=f"game-voice-control-{room_name}",
                )
                return
            if topic != TOPIC_CHAT_DATA:
                return
            try:
                payload = json.loads(bytes(packet.data).decode("utf-8"))
                text = str(payload.get("text", "")).strip()
                raw_cid = payload.get("clientMessageId")
                cid = raw_cid.strip() if isinstance(raw_cid, str) else None
            except Exception:
                return
            if not text:
                return
            if not session_ready.is_set():
                ack: dict[str, Any] = {"type": "user_chat_received", "text": text}
                if cid:
                    ack["clientMessageId"] = cid
                asyncio.create_task(publish_agent_event(ack), name=f"game-early-ack-{room_name}")
                early_chat_queue.append((text, cid))
                return
            asyncio.create_task(
                _process_chat(text, cid, skip_ack=False),
                name=f"game-chat-{room_name}",
            )

        room_options = room_io.RoomOptions()

        try:
            await session.start(pipeline, room=ctx.room, room_options=room_options)
        except TypeError as exc:
            if "room_options" not in str(exc):
                raise
            await session.start(pipeline, room=ctx.room)

        session_ready.set()
        for queued_text, queued_cid in early_chat_queue:
            asyncio.create_task(
                _process_chat(queued_text, queued_cid, skip_ack=True),
                name=f"game-chat-queued-{room_name}",
            )
        early_chat_queue.clear()
        for queued_action in early_voice_queue:
            asyncio.create_task(
                _process_voice_control(queued_action),
                name=f"game-voice-queued-{room_name}",
            )
        early_voice_queue.clear()
        for queued_event_type, queued_payload in early_game_queue:
            asyncio.create_task(
                _process_game_event(queued_event_type, queued_payload),
                name=f"game-event-queued-{room_name}",
            )
        early_game_queue.clear()

        _reset_idle_timer()
        await close_event.wait()
    except Exception as exc:
        logger.error("agent.entry.game.error", room=room_name, error=get_error_message(exc))
    finally:
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await idle_task
        if latency_task is not None and not latency_task.done():
            latency_task.cancel()
            with suppress(asyncio.CancelledError):
                await latency_task
        if session is not None:
            with suppress(Exception):
                await session.aclose()
        close_database(db)


def _start_provider_latency_task(
    publish: Any,
    stt_config: Any,
    llm_config: Any,
    tts_config: Any,
    *,
    name: str,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        publish_provider_latency_loop(publish, stt_config, llm_config, tts_config),
        name=name,
    )
    task.add_done_callback(_log_provider_latency_task_result)
    return task


def _log_provider_latency_task_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.warn("provider-latency.probe.error", error=get_error_message(exc))


def _log_worker_task_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.error("agent.worker.error", error=get_error_message(exc))


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _float_or_none(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None
