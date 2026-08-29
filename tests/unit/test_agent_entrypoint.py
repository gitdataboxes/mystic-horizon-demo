"""Tests for the agent worker entrypoint lifecycle.

Verifies that _agent_entrypoint correctly wires LiveKit session events to the
transcript collector and triggers end-of-call handling on close.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import unittest
from contextlib import AbstractContextManager
from types import SimpleNamespace, TracebackType
from typing import Any, Callable, cast
from unittest.mock import AsyncMock, MagicMock, patch

from mystic.types import Call
from mystic.db import initialize_schema


EventHandler = Callable[[object], object]


def _make_call(call_id: str = "call-1", person_id: str = "person-1") -> Call:
    return Call(
        id=call_id,
        external_id="ext-1",
        person_id=person_id,
        direction="inbound",
        channel="phone",
        modality="voice",
        audience="public",
        action_id=None,
        transcript=None,
        summary=None,
        facts_extracted=0,
        commitments_extracted=0,
        extraction_retries=0,
        extraction_error=None,
        last_extraction_attempt_at=None,
        started_at=1000,
        answered_at=None,
        ended_at=None,
        duration=None,
    )


def _make_ctx(metadata: dict[str, object] | None = None, room_name: str = "call-abc") -> Any:
    """Build a fake JobContext with room metadata."""
    meta_json = json.dumps(metadata) if metadata is not None else json.dumps({
        "callId": "call-1",
        "personId": "person-1",
        "audience": "public",
        "direction": "inbound",
        "channel": "phone",
        "modality": "voice",
        "systemPrompt": "You are a helpful agent.",
        "voiceId": "Mark",
    })
    room_handlers: dict[str, list[EventHandler]] = {}
    room = MagicMock()
    room._event_handlers = room_handlers

    def on(event: str) -> Callable[[EventHandler], EventHandler]:
        def decorator(fn: EventHandler) -> EventHandler:
            room_handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    room.on.side_effect = on
    room.local_participant.publish_data = AsyncMock()
    ctx = SimpleNamespace(
        job=SimpleNamespace(room=SimpleNamespace(metadata=meta_json, name=room_name)),
        connect=AsyncMock(),
        wait_for_participant=AsyncMock(),
        room=room,
        add_shutdown_callback=MagicMock(),
    )
    return ctx


class _SessionStub:
    """Captures event handlers registered via session.on/once decorators."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[EventHandler]] = {}
        self.started = False
        self.closed = False
        self.start_args: tuple[object, ...] = ()
        self.start_kwargs: dict[str, object] = {}
        self.say_handle = AsyncMock()
        self.say_handle.wait_for_playout = AsyncMock()
        self.interrupt = AsyncMock()
        self.generate_reply_calls: list[dict[str, object]] = []
        self.userdata: object = None
        self.history = _HistoryStub()
        self.current_agent = _CurrentAgentStub()
        self.room_io = _RoomIOStub(user_has_audio=False)
        self.output = _AudioToggleStub()
        self.input = _AudioToggleStub()

    def on(self, event: str) -> Callable[[EventHandler], EventHandler]:
        def decorator(fn: EventHandler) -> EventHandler:
            self.handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    def once(self, event: str) -> Callable[[EventHandler], EventHandler]:
        return self.on(event)

    async def start(self, pipeline: object, *, room: object = None, room_options: object = None) -> None:
        self.started = True
        self.start_args = (pipeline,)
        self.start_kwargs = {"room": room, "room_options": room_options}

    def say(self, text: str, *, allow_interruptions: bool = True) -> object:
        return self.say_handle

    def generate_reply(self, **kwargs: object) -> object:
        self.generate_reply_calls.append(kwargs)
        return MagicMock()

    async def aclose(self) -> None:
        self.closed = True


class _AudioToggleStub:
    def __init__(self) -> None:
        self.audio_enabled = True

    def set_audio_enabled(self, enabled: bool) -> None:
        self.audio_enabled = enabled


class _HistoryMessage(SimpleNamespace):
    id: str
    role: str
    text_content: str


class _HistoryStub:
    def __init__(self, messages: list[_HistoryMessage] | None = None, next_id: int = 1) -> None:
        self._messages: list[_HistoryMessage] = list(messages or [])
        self._next_id = next_id

    def add_message(self, *, role: str, content: str) -> _HistoryMessage:
        message = _HistoryMessage(
            id=f"msg-{self._next_id}",
            role=role,
            text_content=content,
        )
        self._next_id += 1
        self._messages.append(message)
        return message

    def messages(self) -> list[_HistoryMessage]:
        return list(self._messages)

    def to_provider_format(self, fmt: str, **kwargs: object) -> tuple[list[dict[str, object]], None]:
        msgs: list[dict[str, object]] = []
        for m in self._messages:
            msgs.append({"role": m.role, "content": m.text_content})
        return msgs, None

    def copy(self) -> _HistoryStub:
        return _HistoryStub(
            messages=[
                _HistoryMessage(
                    id=message.id,
                    role=message.role,
                    text_content=message.text_content,
                )
                for message in self._messages
            ],
            next_id=self._next_id,
        )

    def _upsert_item(self, item: _HistoryMessage) -> None:
        for index, existing in enumerate(self._messages):
            if existing.id == item.id:
                self._messages[index] = item
                return
        self._messages.append(item)


class _RoomIOStub:
    """Stub for session.room_io with controllable linked_participant audio state."""

    def __init__(self, *, user_has_audio: bool = False) -> None:
        self._user_has_audio = user_has_audio
        self.set_participant_calls: list[str | None] = []

    def set_participant(self, participant_identity: str | None) -> None:
        self.set_participant_calls.append(participant_identity)

    @property
    def linked_participant(self) -> SimpleNamespace | None:
        if not self._user_has_audio:
            return SimpleNamespace(track_publications={})
        pub = SimpleNamespace(kind="audio", muted=False)
        return SimpleNamespace(track_publications={"audio-track": pub})


class _CurrentAgentStub:
    def __init__(self) -> None:
        self._chat_ctx = _HistoryStub()
        self.update_chat_ctx = AsyncMock(side_effect=self._set_chat_ctx)

    @property
    def chat_ctx(self) -> _HistoryStub:
        return self._chat_ctx.copy()

    async def _set_chat_ctx(self, chat_ctx: _HistoryStub, **_: object) -> None:
        self._chat_ctx = chat_ctx.copy()


def _fire_close(session_stub: _SessionStub) -> None:
    """Fire the close event on a session stub."""
    close_handlers = session_stub.handlers.get("close", [])
    for handler in close_handlers:
        handler(SimpleNamespace())


async def _fire_user_input(session_stub: _SessionStub, text: str, *, is_final: bool = True) -> None:
    """Fire user_input_transcribed event."""
    event = SimpleNamespace(is_final=is_final, transcript=text)
    for handler in session_stub.handlers.get("user_input_transcribed", []):
        result = handler(event)
        if inspect.isawaitable(result):
            await result


async def _fire_conversation_item(session_stub: _SessionStub, text: str, role: str = "assistant") -> None:
    """Fire conversation_item_added event with a ChatMessage-like item."""
    item = SimpleNamespace(role=role, text_content=text)
    # Make isinstance check pass for llm.ChatMessage
    event = SimpleNamespace(item=item)
    for handler in session_stub.handlers.get("conversation_item_added", []):
        result = handler(event)
        if inspect.isawaitable(result):
            await result


async def _fire_text_input(session_stub: _SessionStub, text: str, *, stream_id: str | None = None) -> None:
    """Trigger the registered RoomIO text-input callback."""
    room_options = cast(Any, session_stub.start_kwargs["room_options"])
    text_input = room_options.get_text_input_options()
    assert text_input is not None
    event = SimpleNamespace(
        text=text,
        info=SimpleNamespace(stream_id=stream_id) if stream_id is not None else None,
    )
    result = text_input.text_input_cb(session_stub, event)
    if inspect.isawaitable(result):
        await result


async def _fire_data_channel_chat(
    ctx: Any,
    text: str,
    *,
    client_message_id: str | None = None,
) -> None:
    """Trigger the LiveKit data-channel chat handler."""
    payload: dict[str, object] = {"text": text}
    if client_message_id is not None:
        payload["clientMessageId"] = client_message_id
    packet = SimpleNamespace(
        topic="mh.chat",
        data=json.dumps(payload).encode("utf-8"),
    )
    handlers = getattr(ctx.room, "_event_handlers", {}).get("data_received", [])
    for handler in handlers:
        result = handler(packet)
        if inspect.isawaitable(result):
            await result
    await asyncio.sleep(0.05)


async def _fire_data_channel_packet(
    ctx: Any,
    *,
    topic: str,
    data: bytes = b"",
) -> None:
    """Trigger a generic LiveKit data-channel packet."""
    packet = SimpleNamespace(topic=topic, data=data)
    handlers = getattr(ctx.room, "_event_handlers", {}).get("data_received", [])
    for handler in handlers:
        result = handler(packet)
        if inspect.isawaitable(result):
            await result


async def _fire_voice_control(ctx: Any, action: str) -> None:
    """Trigger the dashboard voice-control data-channel handler."""
    packet = SimpleNamespace(
        topic="mh.voice_control",
        data=json.dumps({"action": action}).encode("utf-8"),
        participant=SimpleNamespace(identity="dashboard-browser-1"),
    )
    handlers = getattr(ctx.room, "_event_handlers", {}).get("data_received", [])
    for handler in handlers:
        result = handler(packet)
        if inspect.isawaitable(result):
            await result
    await asyncio.sleep(0.05)


def _published_agent_events(ctx: Any) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for call in ctx.room.local_participant.publish_data.await_args_list:
        payload = call.args[0]
        if isinstance(payload, bytes):
            events.append(json.loads(payload.decode("utf-8")))
    return events


class AgentEntrypointTests(unittest.IsolatedAsyncioTestCase):
    """Tests for mystic.worker._agent_entrypoint."""

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        initialize_schema(self.db)
        self.session_stub = _SessionStub()
        self.end_of_call_mock = AsyncMock()

    def tearDown(self) -> None:
        self.db.close()

    def _base_patches(self, call: Call | None = None) -> "_CombinedPatches":
        """Return a combined context manager for all standard mocks."""
        session_stub = self.session_stub

        def make_session(userdata: object = None, **kwargs: object) -> _SessionStub:
            session_stub.userdata = userdata
            return session_stub

        fake_pipeline = MagicMock(name="fake-pipeline")

        return _CombinedPatches(
            patch("mystic.worker.open_database", return_value=self.db),
            patch("mystic.worker.close_database"),
            patch("mystic.worker.init_skills"),
            patch("mystic.worker.get_call_by_id", return_value=call),
            patch("mystic.worker.get_stt_config", return_value=SimpleNamespace(provider="moonshine", model="small")),
            patch("mystic.worker.get_tts_config", return_value=SimpleNamespace(provider="pocket", model=None, pythonCommand=None)),
            patch("mystic.worker.get_realtime_llm_config", return_value=SimpleNamespace(baseURL="http://localhost", apiKey="k", model="m")),
            patch("mystic.worker.create_pipeline", new=AsyncMock(return_value=fake_pipeline)),
            patch("mystic.worker._build_turn_handling", return_value={}),
            patch("mystic.worker.AgentSession", side_effect=make_session),
            patch("mystic.calls.handle_end_of_call_report_by_call_id", new=self.end_of_call_mock),
            patch("mystic.worker.append_call_transcript"),
            # Make isinstance(item, llm.ChatMessage) always True for our stubs
            patch("mystic.worker.llm.ChatMessage", new=type(SimpleNamespace())),
        )

    async def test_entrypoint_connects_and_starts_session(self) -> None:
        """Session connects to room, waits for participant, and starts the pipeline."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        async def run_entrypoint() -> None:
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        with self._base_patches(call):
            await run_entrypoint()

        ctx.connect.assert_awaited_once()
        ctx.wait_for_participant.assert_awaited_once()
        self.assertTrue(self.session_stub.started)
        self.assertEqual(self.session_stub.start_kwargs["room"], ctx.room)

    async def test_user_speech_event_feeds_transcript(self) -> None:
        """user_input_transcribed events are recorded in the transcript collector."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        captured_transcript: list[str] = []

        original_end = self.end_of_call_mock

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_transcript.append(transcript)
            await original_end(db, call_id, transcript, duration)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_user_input(self.session_stub, "Hello, I need help")
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertTrue(len(captured_transcript) > 0)
        self.assertIn("Hello, I need help", captured_transcript[0])
        ctx.room.local_participant.publish_data.assert_awaited_once_with(
            json.dumps(
                {
                    "type": "user_input_transcribed",
                    "transcript": "Hello, I need help",
                    "is_final": True,
                }
            ).encode("utf-8"),
            topic="lk.agent.events",
            reliable=True,
        )

    async def test_agent_speech_event_feeds_transcript(self) -> None:
        """conversation_item_added events with assistant role are recorded and mirrored to HUD."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        captured_transcript: list[str] = []

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_transcript.append(transcript)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_conversation_item(self.session_stub, "Sure, I can help with that")
                await asyncio.sleep(0.05)
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertTrue(len(captured_transcript) > 0)
        self.assertIn("Sure, I can help with that", captured_transcript[0])
        events = _published_agent_events(ctx)
        self.assertEqual(
            events,
            [
                {
                    "type": "agent_voice_transcribed",
                    "transcript": "Sure, I can help with that",
                    "is_final": True,
                }
            ],
        )

    async def test_text_input_without_audio_generates_session_reply(self) -> None:
        """Text chat without active mic routes through the shared AgentSession."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        self.session_stub.room_io = _RoomIOStub(user_has_audio=False)

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            await _fire_text_input(self.session_stub, "Remember the budget draft", stream_id="stream-1")
            await _fire_conversation_item(self.session_stub, "Sure, noted.")
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            self.session_stub.generate_reply_calls,
            [{"user_input": "Remember the budget draft"}],
        )
        self.session_stub.interrupt.assert_awaited_once()
        events = _published_agent_events(ctx)
        self.assertNotIn("agent_chat_response", [event.get("type") for event in events])
        self.assertNotIn("conversation_item_added", [event.get("type") for event in events])

    async def test_text_input_with_audio_still_generates_session_reply(self) -> None:
        """Text chat uses the shared AgentSession even when mic is active."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        self.session_stub.room_io = _RoomIOStub(user_has_audio=True)

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            await _fire_text_input(self.session_stub, "Remember the budget draft", stream_id="stream-1")
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            self.session_stub.generate_reply_calls,
            [{"user_input": "Remember the budget draft"}],
        )
        self.session_stub.interrupt.assert_awaited_once()

    async def test_chat_room_keeps_transcription_output_without_audio(self) -> None:
        """Chat-only rooms still publish native transcription text streams."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "public",
            "direction": "inbound",
            "channel": "dashboard",
            "modality": "text",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
        })
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        room_options = cast(Any, self.session_stub.start_kwargs["room_options"])
        self.assertIsNone(room_options.get_audio_input_options())
        self.assertIsNone(room_options.get_audio_output_options())
        self.assertIsNotNone(room_options.get_text_output_options())

    async def test_dashboard_inbound_starts_with_audio_disabled(self) -> None:
        """Dashboard owner sessions start text-first with LiveKit audio muted."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "owner",
            "direction": "inbound",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
            "chatCallId": "dashboard-chat",
        })
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertFalse(self.session_stub.output.audio_enabled)
        self.assertFalse(self.session_stub.input.audio_enabled)
        room_options = cast(Any, self.session_stub.start_kwargs["room_options"])
        self.assertFalse(room_options.close_on_disconnect)

    async def test_voice_control_start_stop_toggles_audio(self) -> None:
        """Dashboard voice-control packets turn session audio on and off."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "owner",
            "direction": "inbound",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
            "chatCallId": "dashboard-chat",
        })
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            self.assertFalse(self.session_stub.output.audio_enabled)
            self.assertFalse(self.session_stub.input.audio_enabled)

            await _fire_voice_control(ctx, "start")
            self.assertTrue(self.session_stub.output.audio_enabled)
            self.assertTrue(self.session_stub.input.audio_enabled)
            self.assertEqual(
                self.session_stub.room_io.set_participant_calls,
                ["dashboard-browser-1"],
            )

            await _fire_voice_control(ctx, "stop")
            self.assertFalse(self.session_stub.output.audio_enabled)
            self.assertFalse(self.session_stub.input.audio_enabled)

            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

    async def test_dashboard_owner_text_session_keeps_audio_ready_for_voice_toggle(self) -> None:
        """Text-first dashboard owner sessions still create audio I/O for MIC."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "owner",
            "direction": "inbound",
            "channel": "dashboard",
            "modality": "text",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
            "chatCallId": "dashboard-chat",
        })
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            room_options = cast(Any, self.session_stub.start_kwargs["room_options"])
            self.assertIsNotNone(room_options.get_audio_input_options())
            self.assertIsNotNone(room_options.get_audio_output_options())
            self.assertFalse(self.session_stub.output.audio_enabled)
            self.assertFalse(self.session_stub.input.audio_enabled)

            await _fire_voice_control(ctx, "start")
            self.assertTrue(self.session_stub.output.audio_enabled)
            self.assertTrue(self.session_stub.input.audio_enabled)

            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

    async def test_non_dashboard_call_keeps_audio_enabled(self) -> None:
        """Public phone-style rooms are not muted by the dashboard text-first flow."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "public",
            "direction": "inbound",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
        })
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertTrue(self.session_stub.output.audio_enabled)
        self.assertTrue(self.session_stub.input.audio_enabled)

    async def test_dashboard_agent_speech_records_text_modality(self) -> None:
        """Agent transcript entries use text modality while dashboard audio is muted."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "owner",
            "direction": "inbound",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
        })
        call = _make_call()
        captured_transcript: list[str] = []

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_transcript.append(transcript)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_conversation_item(self.session_stub, "Text-first hello")
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertEqual(len(captured_transcript), 1)
        self.assertIn("Agent [text]: Text-first hello", captured_transcript[0])

    async def test_data_channel_client_message_id_is_echoed(self) -> None:
        """Data-channel chat echoes clientMessageId in the delivery ack."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            await _fire_data_channel_chat(
                ctx,
                "Remember the budget draft",
                client_message_id="client-msg-1",
            )
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        received = [
            event for event in _published_agent_events(ctx)
            if event.get("type") == "user_chat_received"
        ]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["text"], "Remember the budget draft")
        self.assertEqual(received[0]["clientMessageId"], "client-msg-1")
        self.assertEqual(
            self.session_stub.generate_reply_calls,
            [{"user_input": "Remember the budget draft"}],
        )

    async def test_data_channel_chat_during_session_start_is_acked_and_drained(self) -> None:
        """Chat sent while the pipeline is warming still gets delivery acked."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        start_entered = asyncio.Event()
        allow_start = asyncio.Event()

        async def delayed_start(
            pipeline: object,
            *,
            room: object = None,
            room_options: object = None,
        ) -> None:
            self.session_stub.started = True
            self.session_stub.start_args = (pipeline,)
            self.session_stub.start_kwargs = {"room": room, "room_options": room_options}
            start_entered.set()
            await allow_start.wait()

        self.session_stub.start = delayed_start  # type: ignore[method-assign]

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.wait_for(start_entered.wait(), timeout=2)

            await _fire_data_channel_chat(
                ctx,
                "Sent before the room was fully warm",
                client_message_id="client-msg-early",
            )
            allow_start.set()
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        received = [
            event for event in _published_agent_events(ctx)
            if event.get("type") == "user_chat_received"
        ]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["clientMessageId"], "client-msg-early")
        self.assertEqual(
            self.session_stub.generate_reply_calls,
            [{"user_input": "Sent before the room was fully warm"}],
        )

    async def test_data_channel_duplicate_client_message_id_is_ignored(self) -> None:
        """A repeated clientMessageId is deduped before running the text reply."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            await _fire_data_channel_chat(
                ctx,
                "Remember the budget draft",
                client_message_id="client-msg-1",
            )
            await _fire_data_channel_chat(
                ctx,
                "Remember the budget draft",
                client_message_id="client-msg-1",
            )
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            self.session_stub.generate_reply_calls,
            [{"user_input": "Remember the budget draft"}],
        )
        received = [
            event for event in _published_agent_events(ctx)
            if event.get("type") == "user_chat_received"
        ]
        self.assertEqual(len(received), 1)

    async def test_non_chat_data_channel_activity_resets_idle_timer(self) -> None:
        """Heartbeat packets keep the shared session alive without becoming chat input."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        with self._base_patches(call):
            with patch("mystic.worker.IDLE_SESSION_TIMEOUT_SECONDS", 0.2):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                for _ in range(50):
                    if getattr(ctx.room, "_event_handlers", {}).get("data_received"):
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.11)

                await _fire_data_channel_packet(ctx, topic="mh.ping")
                await asyncio.sleep(0.12)

                self.assertFalse(self.session_stub.closed)
                self.assertEqual(self.session_stub.generate_reply_calls, [])
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

    async def test_tool_events_are_published_to_room(self) -> None:
        """Tool activity is forwarded to the browser event stream."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)

            on_tool_event = getattr(self.session_stub.userdata, "on_tool_event", None)
            self.assertIsNotNone(on_tool_event)
            assert on_tool_event is not None
            await on_tool_event("tool_started", "read-calendar")

            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        ctx.room.local_participant.publish_data.assert_awaited_once_with(
            json.dumps({"type": "tool_started", "name": "read-calendar"}).encode("utf-8"),
            topic="lk.agent.events",
            reliable=True,
        )

    async def test_dashboard_tool_events_are_persisted_for_history(self) -> None:
        """Dashboard tool activity is written into transcript history for refresh replay."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx({
            "callId": "call-1",
            "personId": "person-1",
            "audience": "owner",
            "direction": "inbound",
            "channel": "dashboard",
            "modality": "voice",
            "systemPrompt": "You are a helpful agent.",
            "voiceId": "Mark",
            "chatCallId": "dashboard-chat",
        })
        call = _make_call()

        with self._base_patches(call):
            with patch("mystic.worker.append_call_transcript") as append_transcript:
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                on_tool_event = getattr(self.session_stub.userdata, "on_tool_event", None)
                self.assertIsNotNone(on_tool_event)
                assert on_tool_event is not None
                await on_tool_event("tool_started", "read-calendar", args_summary="today")

                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        append_transcript.assert_any_call(
            self.db,
            "call-1",
            '[0:00] Tool [event]: {"type":"tool_started","name":"read-calendar","args_summary":"today"}',
        )
        self.assertTrue(
            any(
                call_args.args[1] == "dashboard-chat"
                and str(call_args.args[2]).endswith(
                    'Tool [event]: {"type":"tool_started","name":"read-calendar","args_summary":"today"}'
                )
                for call_args in append_transcript.call_args_list
            )
        )

    async def test_close_event_persists_transcript_and_triggers_end_of_call(self) -> None:
        """Closing the session persists the full transcript and calls handle_end_of_call_report_by_call_id."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        captured_args: list[tuple[str, str, int]] = []

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_args.append((call_id, transcript, duration))

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_user_input(self.session_stub, "What time is the meeting?")
                await _fire_conversation_item(self.session_stub, "The meeting is at 3 PM.")
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        # End-of-call was invoked with the right call_id
        self.assertEqual(len(captured_args), 1)
        self.assertEqual(captured_args[0][0], "call-1")
        transcript: str = captured_args[0][1]
        self.assertIn("What time is the meeting?", transcript)
        self.assertIn("The meeting is at 3 PM.", transcript)

    async def test_entrypoint_returns_early_when_no_person_id(self) -> None:
        """If call_id resolves but no person_id can be derived, entrypoint exits without crash."""
        from mystic.worker import _agent_entrypoint

        # Metadata has callId but no personId, and get_call_by_id returns None
        ctx = _make_ctx(metadata={
            "callId": "call-orphan",
            "audience": "public",
            "direction": "inbound",
            "systemPrompt": "test",
        })

        with self._base_patches(call=None):
            await _agent_entrypoint(ctx)

        # Session should never have started — no person_id to work with
        self.assertFalse(self.session_stub.started)

    async def test_entrypoint_exits_when_no_call_id(self) -> None:
        """If room metadata has no callId and room name doesn't match pattern, entrypoint exits."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={}, room_name="no-match")

        with self._base_patches():
            await _agent_entrypoint(ctx)

        self.assertFalse(self.session_stub.started)

    async def test_bootstrap_generates_first_reply(self) -> None:
        """Bootstrap sessions start with an interruptible generated reply."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "public",
            "direction": "inbound",
            "systemPrompt": "Hello",
            "voiceId": "Mark",
            "bootstrap": True,
        })
        call = _make_call()
        said: list[str] = []
        original_say = self.session_stub.say

        def capturing_say(text: str, *, allow_interruptions: bool = True) -> object:
            said.append(text)
            return original_say(text, allow_interruptions=allow_interruptions)

        self.session_stub.say = capturing_say

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(said, [])
        self.assertEqual(self.session_stub.generate_reply_calls, [{}])

    async def test_non_bootstrap_first_message_is_spoken(self) -> None:
        """If room metadata contains firstMessage, session.say() is called."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx(metadata={
            "callId": "call-1",
            "personId": "person-1",
            "audience": "public",
            "direction": "inbound",
            "systemPrompt": "Hello",
            "voiceId": "Mark",
            "firstMessage": "Hi, this is TestBot calling!",
        })
        call = _make_call()
        said: list[str] = []
        original_say = self.session_stub.say

        def capturing_say(text: str, *, allow_interruptions: bool = True) -> object:
            said.append(text)
            return original_say(text, allow_interruptions=allow_interruptions)

        self.session_stub.say = capturing_say

        with self._base_patches(call):
            task = asyncio.create_task(_agent_entrypoint(ctx))
            await asyncio.sleep(0.05)
            _fire_close(self.session_stub)
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(said, ["Hi, this is TestBot calling!"])
        self.assertEqual(len(self.session_stub.generate_reply_calls), 0)

    async def test_non_final_user_input_is_ignored(self) -> None:
        """Interim (non-final) STT results are not added to the transcript."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        captured_transcript: list[str] = []

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_transcript.append(transcript)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_user_input(self.session_stub, "interim text", is_final=False)
                await _fire_user_input(self.session_stub, "final text", is_final=True)
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertTrue(len(captured_transcript) > 0)
        self.assertNotIn("interim text", captured_transcript[0])
        self.assertIn("final text", captured_transcript[0])
        ctx.room.local_participant.publish_data.assert_awaited_once_with(
            json.dumps(
                {
                    "type": "user_input_transcribed",
                    "transcript": "final text",
                    "is_final": True,
                }
            ).encode("utf-8"),
            topic="lk.agent.events",
            reliable=True,
        )

    async def test_pipeline_error_publishes_agent_error(self) -> None:
        """If create_pipeline raises, agent publishes agent_error and exits without starting session."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()

        with self._base_patches(call):
            with patch("mystic.worker.create_pipeline", new=AsyncMock(side_effect=RuntimeError("TTS not configured"))):
                await _agent_entrypoint(ctx)

        self.assertFalse(self.session_stub.started)
        ctx.room.local_participant.publish_data.assert_awaited_once()
        sent = json.loads(ctx.room.local_participant.publish_data.call_args[0][0].decode("utf-8"))
        self.assertEqual(sent["type"], "agent_error")
        self.assertIn("TTS not configured", sent["message"])

    async def test_non_assistant_conversation_item_is_ignored(self) -> None:
        """Only assistant-role conversation items are added to the transcript."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        captured_transcript: list[str] = []

        async def capture_end(
            db: sqlite3.Connection,
            call_id: str,
            transcript: str,
            duration: int,
        ) -> None:
            captured_transcript.append(transcript)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=capture_end):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                await _fire_conversation_item(self.session_stub, "system message", role="system")
                await _fire_conversation_item(self.session_stub, "assistant reply", role="assistant")
                await asyncio.sleep(0.05)
                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertTrue(len(captured_transcript) > 0)
        self.assertNotIn("system message", captured_transcript[0])
        self.assertIn("assistant reply", captured_transcript[0])
        events = _published_agent_events(ctx)
        self.assertEqual(
            events,
            [
                {
                    "type": "agent_voice_transcribed",
                    "transcript": "assistant reply",
                    "is_final": True,
                }
            ],
        )

    async def test_session_start_falls_back_when_room_options_unsupported(self) -> None:
        """session.start() retries without room_options when the SDK rejects them."""
        from mystic.worker import _agent_entrypoint

        ctx = _make_ctx()
        call = _make_call()
        start_calls: list[dict[str, object]] = []

        async def start_with_room_options_fallback(
            pipeline: object,
            *,
            room: object = None,
            room_options: object = None,
        ) -> None:
            start_calls.append({"room": room, "room_options": room_options})
            if len(start_calls) == 1:
                raise TypeError("session.start() got an unexpected keyword argument 'room_options'")
            self.session_stub.started = True
            self.session_stub.start_args = (pipeline,)
            self.session_stub.start_kwargs = {"room": room, "room_options": room_options}

        self.session_stub.start = AsyncMock(side_effect=start_with_room_options_fallback)

        with self._base_patches(call):
            with patch("mystic.calls.handle_end_of_call_report_by_call_id", new=AsyncMock()):
                task = asyncio.create_task(_agent_entrypoint(ctx))
                await asyncio.sleep(0.05)

                _fire_close(self.session_stub)
                await asyncio.wait_for(task, timeout=2)

        self.assertEqual(len(start_calls), 2)
        self.assertIsNotNone(start_calls[0]["room_options"])
        self.assertIsNone(start_calls[1]["room_options"])
        self.assertTrue(self.session_stub.started)


class TurnHandlingConfigTests(unittest.TestCase):
    """Tests for the worker turn-handling configuration helper."""

    def test_build_turn_handling_with_plugin(self) -> None:
        import mystic.worker as worker

        turn_detector = object()
        module = SimpleNamespace(MultilingualModel=MagicMock(return_value=turn_detector))

        with patch.object(worker.importlib, "import_module", return_value=module):
            config = worker._build_turn_handling()

        self.assertIs(config.get("turn_detection"), turn_detector)
        self.assertEqual(
            config.get("endpointing"),
            {
                "mode": "dynamic",
                "min_delay": 0.2,
                "max_delay": 1.5,
            },
        )
        self.assertEqual(
            config.get("interruption"),
            {
                "mode": "vad",
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
                "min_duration": 0.3,
                "min_words": 1,
            },
        )
        module.MultilingualModel.assert_called_once_with()

    def test_build_turn_handling_game_mode_loosens_interruption(self) -> None:
        import mystic.worker as worker

        turn_detector = object()
        module = SimpleNamespace(MultilingualModel=MagicMock(return_value=turn_detector))

        with patch.object(worker.importlib, "import_module", return_value=module):
            config = worker._build_turn_handling(game_mode=True)

        self.assertEqual(
            config.get("endpointing"),
            {
                "mode": "dynamic",
                "min_delay": 0.2,
                "max_delay": 1.5,
            },
        )
        self.assertEqual(
            config.get("interruption"),
            {
                "mode": "vad",
                "resume_false_interruption": True,
                "false_interruption_timeout": 3.5,
                "min_duration": 0.6,
                "min_words": 2,
            },
        )

    def test_build_turn_handling_falls_back_when_plugin_missing(self) -> None:
        import mystic.worker as worker

        error = ModuleNotFoundError("No module named 'livekit.plugins.turn_detector'")
        error.name = "livekit.plugins.turn_detector"

        with patch.object(worker.logger, "warn") as warn_mock:
            with patch.object(worker.importlib, "import_module", side_effect=error):
                config = worker._build_turn_handling()

        self.assertEqual(config, {"endpointing": {"min_delay": 0.2}})
        warn_mock.assert_called_once_with(
            "agent.worker.turn-detector.unavailable",
            hint="pip install livekit-plugins-turn-detector",
        )

    def test_build_turn_handling_falls_back_when_plugin_init_fails(self) -> None:
        import mystic.worker as worker

        module = SimpleNamespace(
            MultilingualModel=MagicMock(
                side_effect=RuntimeError('Could not find file "languages.json".')
            )
        )

        with patch.object(worker.logger, "warn") as warn_mock:
            with patch.object(worker.importlib, "import_module", return_value=module):
                config = worker._build_turn_handling()

        self.assertEqual(config, {"endpointing": {"min_delay": 0.2}})
        warn_mock.assert_called_once_with(
            "agent.worker.turn-detector.init-failed",
            error='Could not find file "languages.json".',
            hint="Using endpointing fallback.",
        )


class _CombinedPatches:
    """Combine multiple patch objects into a single context manager."""

    def __init__(self, *patches: AbstractContextManager[object]) -> None:
        self._patches = patches
        self._active: list[object] = []

    def __enter__(self) -> _CombinedPatches:
        for p in self._patches:
            self._active.append(p.__enter__())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for p in reversed(self._patches):
            p.__exit__(exc_type, exc, tb)
        self._active.clear()
