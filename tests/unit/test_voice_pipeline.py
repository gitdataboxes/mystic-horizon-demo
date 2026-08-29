from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, call, patch

import numpy as np

from mystic.skills import init_skills, reset_registry
from mystic.audio import ATTENTION_CUES
from mystic.voice import (
    AgentToolUserData,
    DEFAULT_LLM_MAX_COMPLETION_TOKENS,
    DEFAULT_VOICE,
    MysticAgent,
    POCKET_ONNX_BASE,
    PipelineConfig,
    build_agent_tools,
    collapse_adjacent_repeated_text,
    create_llm,
    create_pipeline,
    create_stt,
    create_transcript_collector,
    _load_pocket_engine,
    _resolve_pocket_voice,
    _to_pcm16_bytes,
)
from mystic.worker import (
    RoomMetadata,
    WorkerConfig,
    compute_worker_load,
    parse_room_metadata,
    resolve_call_id,
    resolve_effective_max_active_jobs,
    resolve_max_active_jobs,
    resolve_worker_server_type,
    start_agent_worker,
    stop_agent_worker,
)
from mystic.config import (
    DeepgramSttConfig,
    InworldTtsConfig,
    MoonshineSttConfig,
    PocketTtsConfig,
    ResolvedLLMConfig,
)
from mystic.types import SkillContext, ToolResult


class FakeClock:
    def __init__(self) -> None:
        self.current = 1_000

    def now_ms(self) -> int:
        return self.current

    def advance(self, ms: int) -> None:
        self.current += ms


class WorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await stop_agent_worker()

    def _worker_config(self) -> WorkerConfig:
        return WorkerConfig(
            livekit_config=cast(Any, SimpleNamespace(
                host="127.0.0.1",
                port=7880,
                apiKey="devkey",
                apiSecret="devsecret",
            )),
            tts_config=cast(Any, SimpleNamespace(provider="inworld")),
            default_voice_id="TestVoice",
            max_active_jobs=3,
        )

    async def test_start_agent_worker_waits_for_livekit_registration(self) -> None:
        class FakeAgentServer:
            created: "FakeAgentServer | None" = None

            def __init__(self) -> None:
                self.listeners: dict[str, list[Any]] = {}
                self.closed = asyncio.Event()
                self.registered = False
                self.drained = False

            @classmethod
            def from_server_options(cls, _options: object) -> "FakeAgentServer":
                cls.created = cls()
                return cls.created

            @property
            def active_jobs(self) -> list[object]:
                return []

            def on(self, event: str, callback: Any = None) -> Any:
                self.listeners.setdefault(event, []).append(callback)
                return callback

            def off(self, event: str, callback: Any) -> None:
                callbacks = self.listeners.get(event, [])
                if callback in callbacks:
                    callbacks.remove(callback)

            def emit(self, event: str, *args: object) -> None:
                for callback in list(self.listeners.get(event, [])):
                    callback(*args)

            async def run(self) -> None:
                await asyncio.sleep(0)
                self.registered = True
                self.emit("worker_registered", "worker-1", SimpleNamespace())
                await self.closed.wait()

            async def drain(self, timeout: int = 0) -> None:
                self.drained = True

            async def aclose(self) -> None:
                self.closed.set()

        with patch("mystic.worker.AgentServer", FakeAgentServer):
            await start_agent_worker(self._worker_config())

        assert FakeAgentServer.created is not None
        self.assertTrue(FakeAgentServer.created.registered)
        self.assertFalse(FakeAgentServer.created.drained)

    async def test_start_agent_worker_times_out_if_worker_never_registers(self) -> None:
        class FakeAgentServer:
            created: "FakeAgentServer | None" = None

            def __init__(self) -> None:
                self.listeners: dict[str, list[Any]] = {}
                self.closed = asyncio.Event()
                self.closed_by_startup_failure = False

            @classmethod
            def from_server_options(cls, _options: object) -> "FakeAgentServer":
                cls.created = cls()
                return cls.created

            @property
            def active_jobs(self) -> list[object]:
                return []

            def on(self, event: str, callback: Any = None) -> Any:
                self.listeners.setdefault(event, []).append(callback)
                return callback

            def off(self, event: str, callback: Any) -> None:
                callbacks = self.listeners.get(event, [])
                if callback in callbacks:
                    callbacks.remove(callback)

            async def run(self) -> None:
                await self.closed.wait()

            async def drain(self, timeout: int = 0) -> None:
                pass

            async def aclose(self) -> None:
                self.closed_by_startup_failure = True
                self.closed.set()

        with (
            patch("mystic.worker.AgentServer", FakeAgentServer),
            patch("mystic.worker.WORKER_REGISTRATION_TIMEOUT_SECONDS", 0.01),
        ):
            with self.assertRaises(TimeoutError):
                await start_agent_worker(self._worker_config())

        assert FakeAgentServer.created is not None
        self.assertTrue(FakeAgentServer.created.closed_by_startup_failure)


class TranscriptCollectorTests(unittest.TestCase):
    def test_collapse_adjacent_repeated_text_removes_duplicate_paragraph(self) -> None:
        text = "Hey — what’s your name?\n\nHey — what’s your name?"

        self.assertEqual(
            collapse_adjacent_repeated_text(text),
            "Hey — what’s your name?",
        )

    def test_collapse_adjacent_repeated_text_keeps_short_emphasis(self) -> None:
        text = "No.\n\nNo."

        self.assertEqual(collapse_adjacent_repeated_text(text), text)

    def test_collapse_adjacent_repeated_text_keeps_distinct_paragraphs(self) -> None:
        text = "Hey — what’s your name?\n\nThen we’ll pick mine."

        self.assertEqual(collapse_adjacent_repeated_text(text), text)

    def test_deduplicates_consecutive_speech_per_role(self) -> None:
        clock = FakeClock()
        collector = create_transcript_collector(now_ms=clock.now_ms)

        collector.add_agent_speech(" Hello ")
        clock.advance(1_000)
        collector.add_agent_speech("Hello")
        clock.advance(1_000)
        collector.add_user_speech("Hi")
        clock.advance(1_000)
        collector.add_user_speech("Hi")
        clock.advance(1_000)
        collector.add_agent_speech("Hello")

        self.assertEqual(
            collector.to_transcript(),
            "[0:00] Agent: Hello\n[0:02] Caller: Hi",
        )

    def test_peek_and_consume_delta_transcript_track_persisted_cursor(self) -> None:
        clock = FakeClock()
        collector = create_transcript_collector(now_ms=clock.now_ms)

        collector.add_user_speech("First")
        clock.advance(1_000)
        collector.add_agent_speech("Second")

        self.assertEqual(
            collector.peek_delta_transcript(),
            "[0:00] Caller: First\n[0:01] Agent: Second",
        )
        self.assertEqual(
            collector.consume_delta_transcript(),
            "[0:00] Caller: First\n[0:01] Agent: Second",
        )
        self.assertEqual(collector.peek_delta_transcript(), "")

        clock.advance(1_000)
        collector.add_user_speech("Third")

        self.assertEqual(collector.peek_delta_transcript(), "[0:02] Caller: Third")

    def test_reports_duration_in_seconds(self) -> None:
        clock = FakeClock()
        collector = create_transcript_collector(now_ms=clock.now_ms)

        clock.advance(3_200)

        self.assertEqual(collector.get_duration(), 3)

    def test_formats_tool_events_in_transcript_order(self) -> None:
        clock = FakeClock()
        collector = create_transcript_collector(now_ms=clock.now_ms)

        collector.add_user_speech("Check my calendar", modality="text")
        clock.advance(500)
        collector.add_tool_event("tool_started", "read-calendar", {"args_summary": "today"})
        clock.advance(750)
        collector.add_tool_event("tool_completed", "read-calendar", {"duration_ms": 750, "error": False})

        self.assertEqual(
            collector.to_transcript(),
            '[0:00] Caller [text]: Check my calendar\n'
            '[0:00] Tool [event]: {"type":"tool_started","name":"read-calendar","args_summary":"today"}\n'
            '[0:01] Tool [event]: {"type":"tool_completed","name":"read-calendar","duration_ms":750,"error":false}',
        )


class AgentToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        reset_registry()
        init_skills()

    async def asyncTearDown(self) -> None:
        self.db.close()
        reset_registry()

    async def test_public_voice_tools_include_skill_specific_names_and_non_skill_tools(self) -> None:
        tools = build_agent_tools(
            self.db,
            SkillContext(
                audience="public",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id="call-1",
                person_id="person-1",
                source="mid-call",
            ),
        )

        names = {tool.info.name for tool in tools}
        self.assertIn("read-calendar", names)
        self.assertIn("read-transcripts", names)
        self.assertIn("take-message", names)
        self.assertIn("transfer-call", names)
        self.assertIn("hold-call", names)
        self.assertNotIn("say", names)
        self.assertNotIn("display", names)
        self.assertNotIn("notify", names)
        self.assertNotIn("send_text", names)
        self.assertNotIn("design-dashboard", names)
        self.assertNotIn("read-dashboard", names)
        self.assertNotIn("write-fact", names)

        check_availability = next(tool for tool in tools if tool.info.name == "check-availability")
        assert check_availability.info.raw_schema is not None
        raw_schema = check_availability.info.raw_schema
        self.assertEqual(raw_schema["name"], "check-availability")
        self.assertEqual(raw_schema["parameters"]["required"], ["start", "end"])

    async def test_owner_voice_tools_include_owner_and_voice_only_skills(self) -> None:
        tools = build_agent_tools(
            self.db,
            SkillContext(
                audience="owner",
                direction="inbound",
                channel="phone",
                modality="voice",
                call_id="call-1",
                person_id="person-1",
                source="mid-call",
            ),
        )

        names = {tool.info.name for tool in tools}
        self.assertIn("read-soul", names)
        self.assertIn("write-fact", names)
        self.assertIn("edit-soul", names)
        self.assertIn("send-dtmf", names)
        self.assertIn("warm-transfer-call", names)
        self.assertNotIn("design-dashboard", names)
        self.assertNotIn("read-dashboard", names)

    async def test_dashboard_owner_tools_include_text_setup_skills(self) -> None:
        tools = build_agent_tools(
            self.db,
            SkillContext(
                audience="owner",
                direction="inbound",
                channel="dashboard",
                modality="voice",
                call_id="call-1",
                person_id="person-1",
                source="mid-call",
            ),
        )

        names = {tool.info.name for tool in tools}
        self.assertEqual(len(names), len(tools))
        self.assertIn("warm-transfer-call", names)
        self.assertIn("read-twilio-numbers", names)
        self.assertIn("write-twilio-number", names)
        self.assertIn("check-tailscale", names)
        self.assertIn("design-dashboard", names)

    async def test_direct_skill_tool_invocation_uses_raw_arguments_and_run_context_userdata(self) -> None:
        skill_ctx = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-99",
            person_id="person-99",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        read_tool = next(tool for tool in tools if tool.info.name == "read-transcripts")
        run_ctx = SimpleNamespace(userdata=AgentToolUserData(db=self.db, skill_context=skill_ctx))

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-1", result="ok")]),
        ) as execute_tool_calls:
            result = await read_tool(
                run_ctx,
                raw_arguments={"query": "birthday"},
            )

        self.assertEqual(result, "ok")
        assert execute_tool_calls.await_args is not None
        args = execute_tool_calls.await_args.args
        self.assertIs(args[0], self.db)
        self.assertEqual(args[1].call_id, "call-99")
        tool_call = args[2][0]
        self.assertEqual(tool_call.name, "read-transcripts")
        self.assertEqual(tool_call.arguments["query"], "birthday")

    async def test_write_action_tool_invocation_uses_raw_arguments(self) -> None:
        skill_ctx = SkillContext(
            audience="owner",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-77",
            person_id="person-77",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        write_tool = next(tool for tool in tools if tool.info.name == "write-action")
        run_ctx = SimpleNamespace(userdata=AgentToolUserData(db=self.db, skill_context=skill_ctx))

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-2", result="ok")]),
        ) as execute_tool_calls:
            result = await write_tool(
                run_ctx,
                raw_arguments={"intent": "Follow up tomorrow"},
            )

        self.assertEqual(result, "ok")
        assert execute_tool_calls.await_args is not None
        tool_call = execute_tool_calls.await_args.args[2][0]
        self.assertEqual(tool_call.name, "write-action")
        self.assertEqual(tool_call.arguments["intent"], "Follow up tomorrow")

    async def test_edit_soul_tool_invocation_uses_raw_arguments(self) -> None:
        skill_ctx = SkillContext(
            audience="owner",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-55",
            person_id="person-55",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        edit_tool = next(tool for tool in tools if tool.info.name == "edit-soul")
        run_ctx = SimpleNamespace(userdata=AgentToolUserData(db=self.db, skill_context=skill_ctx))

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-3", result="ok")]),
        ) as execute_tool_calls:
            result = await edit_tool(
                run_ctx,
                raw_arguments={"instruction": "Make the tone warmer."},
            )

        self.assertEqual(result, "ok")
        assert execute_tool_calls.await_args is not None
        tool_call = execute_tool_calls.await_args.args[2][0]
        self.assertEqual(tool_call.name, "edit-soul")
        self.assertEqual(tool_call.arguments["instruction"], "Make the tone warmer.")

    async def test_zero_argument_tool_invocation_uses_empty_arguments(self) -> None:
        skill_ctx = SkillContext(
            audience="owner",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-99",
            person_id="person-1",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        read_tool = next(tool for tool in tools if tool.info.name == "read-soul")
        run_ctx = SimpleNamespace(userdata=AgentToolUserData(db=self.db, skill_context=skill_ctx))

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-5", result="ok")]),
        ) as execute_tool_calls:
            result = await read_tool(run_ctx)

        self.assertEqual(result, "ok")
        assert execute_tool_calls.await_args is not None
        tool_call = execute_tool_calls.await_args.args[2][0]
        self.assertEqual(tool_call.name, "read-soul")
        self.assertEqual(tool_call.arguments, {})

    async def test_skill_tool_emits_tool_started_and_completed_events(self) -> None:
        skill_ctx = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-101",
            person_id="person-101",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        read_tool = next(tool for tool in tools if tool.info.name == "read-transcripts")
        on_tool_event = AsyncMock()
        run_ctx = SimpleNamespace(
            userdata=AgentToolUserData(
                db=self.db,
                skill_context=skill_ctx,
                on_tool_event=on_tool_event,
            )
        )

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-4", result="ok")]),
        ):
            result = await read_tool(
                run_ctx,
                raw_arguments={"query": "budget"},
            )

        self.assertEqual(result, "ok")
        on_tool_event.assert_has_awaits([
            call("tool_started", "read-transcripts", args_summary="budget"),
            call("tool_completed", "read-transcripts", duration_ms=ANY, error=False),
        ])

    async def test_skill_tool_summary_includes_twilio_phone_number(self) -> None:
        skill_ctx = SkillContext(
            audience="owner",
            direction="inbound",
            channel="dashboard",
            modality="text",
            call_id="call-101b",
            person_id="person-101b",
            source="owner",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        write_tool = next(tool for tool in tools if tool.info.name == "write-twilio-number")
        on_tool_event = AsyncMock()
        run_ctx = SimpleNamespace(
            userdata=AgentToolUserData(
                db=self.db,
                skill_context=skill_ctx,
                on_tool_event=on_tool_event,
            )
        )

        with patch(
            "mystic.skills.execute_tool_calls",
            new=AsyncMock(return_value=[ToolResult(tool_call_id="tool-4", result="ok")]),
        ):
            result = await write_tool(
                run_ctx,
                raw_arguments={"phone_number": "+15555077192"},
            )

        self.assertEqual(result, "ok")
        on_tool_event.assert_has_awaits([
            call("tool_started", "write-twilio-number", args_summary="+15555077192"),
            call("tool_completed", "write-twilio-number", duration_ms=ANY, error=False),
        ])

    async def test_chat_tool_sends_text_without_skill_handler_dispatch(self) -> None:
        skill_ctx = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-102",
            person_id="person-102",
            source="mid-call",
        )
        tools = build_agent_tools(self.db, skill_ctx)
        chat_tool = next(tool for tool in tools if tool.info.name == "chat")
        on_send_text = AsyncMock()
        on_tool_event = AsyncMock()
        run_ctx = SimpleNamespace(
            userdata=AgentToolUserData(
                db=self.db,
                skill_context=skill_ctx,
                on_send_text=on_send_text,
                on_tool_event=on_tool_event,
            )
        )

        with patch("mystic.voice._skills_execute_tool", new=AsyncMock()) as execute_tool:
            result = await chat_tool(
                run_ctx,
                raw_arguments={"message": "**Details**\n\n- one"},
            )

        self.assertEqual(result, "sent")
        on_send_text.assert_awaited_once_with("**Details**\n\n- one")
        execute_tool.assert_not_awaited()
        on_tool_event.assert_has_awaits([
            call("tool_started", "chat", args_summary="**Details**\n\n- one"),
            call("tool_completed", "chat", duration_ms=ANY, error=False),
        ])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_pipeline_config_accepts_deepgram_stt(self) -> None:
        config = PipelineConfig(
            stt=DeepgramSttConfig(provider="deepgram", apiKey="dg-key"),
            tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
            llm=ResolvedLLMConfig(baseURL="http://llm.local", apiKey="llm-key", model="model-a"),
        )

        self.assertIsInstance(config.stt, DeepgramSttConfig)
        assert isinstance(config.stt, DeepgramSttConfig)
        self.assertEqual(config.stt.apiKey, "dg-key")
        self.assertIsNone(config.stt.model)

    async def test_create_stt_builds_deepgram_plugin_with_default_model(self) -> None:
        captured: dict[str, object] = {}

        def fake_stt(**kwargs: object) -> str:
            captured.update(kwargs)
            return "deepgram-stt"

        plugin = SimpleNamespace(STT=fake_stt)
        with patch("mystic.voice.importlib.import_module", return_value=plugin) as import_module:
            stt = await create_stt(DeepgramSttConfig(provider="deepgram", apiKey="dg-key"))

        import_module.assert_called_once_with("livekit.plugins.deepgram")
        self.assertEqual(captured, {"model": "nova-3", "api_key": "dg-key"})
        self.assertEqual(stt, "deepgram-stt")

    def test_create_llm_caps_completion_tokens(self) -> None:
        config = ResolvedLLMConfig(
            baseURL="https://openrouter.ai/api/v1",
            apiKey="llm-key",
            model="openai/gpt-5.5",
        )

        with patch("mystic.voice.openai.LLM", return_value="llm") as llm_cls:
            llm = create_llm(config)

        self.assertEqual(llm, "llm")
        llm_cls.assert_called_once_with(
            base_url="https://openrouter.ai/api/v1",
            api_key="llm-key",
            model="openai/gpt-5.5",
            max_completion_tokens=DEFAULT_LLM_MAX_COMPLETION_TOKENS,
        )

    async def test_create_pipeline_wires_factories_into_agent(self) -> None:
        db = sqlite3.connect(":memory:")
        skill_ctx = SkillContext(
            audience="owner",
            direction="outbound",
            channel="phone",
            modality="voice",
            call_id="call-2",
            person_id="person-2",
            source="mid-call",
        )
        config = PipelineConfig(
            stt=MoonshineSttConfig(provider="moonshine", model="small"),
            tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
            llm=ResolvedLLMConfig(baseURL="http://llm.local", apiKey="llm-key", model="model-a"),
        )
        seen: dict[str, object] = {}

        async def fake_stt_factory(stt_config: object) -> str:
            seen["stt"] = stt_config
            return "stt"

        async def fake_tts_factory(tts_config: object, voice_id: str | None) -> str:
            seen["tts"] = (tts_config, voice_id)
            return "tts"

        async def fake_vad_loader() -> str:
            return "vad"

        def fake_llm_factory(llm_config: object) -> str:
            seen["llm"] = llm_config
            return "llm"

        class FakeAgent:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        agent = cast(FakeAgent, await create_pipeline(
            config,
            "system prompt",
            "Voice-X",
            db,
            skill_ctx,
            agent_cls=FakeAgent,
            stt_factory=fake_stt_factory,
            tts_factory=fake_tts_factory,
            llm_factory=fake_llm_factory,
            vad_loader=fake_vad_loader,
        ))

        self.assertIs(seen["stt"], config.stt)
        self.assertEqual(seen["tts"], (config.tts, "Voice-X"))
        self.assertIs(seen["llm"], config.llm)
        instructions = cast(str, agent.kwargs["instructions"])
        self.assertTrue(instructions.startswith("system prompt"))
        self.assertEqual(agent.kwargs["stt"], "stt")
        self.assertEqual(agent.kwargs["tts"], "tts")
        self.assertEqual(agent.kwargs["llm"], "llm")
        self.assertEqual(agent.kwargs["vad"], "vad")
        tools = cast(list[Any], agent.kwargs["tools"])
        tool_names = {tool.info.name for tool in tools}
        self.assertGreater(len(tools), 10)
        self.assertIn("write-action", tool_names)
        self.assertNotIn("say", tool_names)
        self.assertNotIn("display", tool_names)
        self.assertNotIn("notify", tool_names)
        self.assertNotIn("send_text", tool_names)
        db.close()

    async def test_create_pipeline_uses_pocket_fallback_voice(self) -> None:
        db = sqlite3.connect(":memory:")
        skill_ctx = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-3",
            person_id="person-3",
            source="mid-call",
        )
        config = PipelineConfig(
            stt=MoonshineSttConfig(provider="moonshine", model="small"),
            tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
            llm=ResolvedLLMConfig(baseURL="http://llm.local", apiKey="llm-key", model="model-a"),
        )
        seen: list[str | None] = []

        async def fake_stt_factory(_: object) -> str:
            return "stt"

        async def fake_tts_factory(_: object, voice_id: str | None) -> str:
            seen.append(voice_id)
            return "tts"

        async def fake_vad_loader() -> str:
            return "vad"

        def fake_llm_factory(_: object) -> str:
            return "llm"

        class FakeAgent:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        await create_pipeline(
            config,
            "prompt",
            None,
            db,
            skill_ctx,
            agent_cls=FakeAgent,
            stt_factory=fake_stt_factory,
            tts_factory=fake_tts_factory,
            llm_factory=fake_llm_factory,
            vad_loader=fake_vad_loader,
        )

        self.assertEqual(seen, [DEFAULT_VOICE])
        db.close()

    async def test_mystic_agent_injects_current_time_on_user_turn(self) -> None:
        from livekit.agents import llm

        from tests.python_helpers import TempAppHome, seed_core_files

        def collect_time_messages(ctx: llm.ChatContext) -> list[llm.ChatMessage]:
            return [
                item for item in ctx.items
                if isinstance(item, llm.ChatMessage)
                and (item.id or "").startswith("mh-time-")
            ]

        with TempAppHome() as home:
            seed_core_files(home)
            agent = cast(MysticAgent, object.__new__(MysticAgent))
            turn_ctx = llm.ChatContext.empty()
            turn_ctx.add_message(role="user", content="first question")
            message = llm.ChatMessage(role="user", content=["first question"])

            await agent.on_user_turn_completed(turn_ctx, message)
            stamps = collect_time_messages(turn_ctx)
            self.assertEqual(len(stamps), 1)
            self.assertEqual(stamps[0].role, "system")
            stamp_content = stamps[0].content
            stamp_text = stamp_content[0] if isinstance(stamp_content, list) else stamp_content
            self.assertIn("Current time:", cast(str, stamp_text))

            await agent.on_user_turn_completed(turn_ctx, message)
            self.assertEqual(len(collect_time_messages(turn_ctx)), 1)


class WorkerHelperTests(unittest.TestCase):
    def test_compute_worker_load_marks_idle_and_full_capacity(self) -> None:
        self.assertEqual(compute_worker_load(0), 0.0)
        self.assertEqual(compute_worker_load(1, 1), 1.0)
        self.assertEqual(compute_worker_load(2, 1), 1.0)
        self.assertEqual(compute_worker_load(0, 0), 1.0)

    def test_resolve_max_active_jobs_prefers_valid_env(self) -> None:
        self.assertEqual(resolve_max_active_jobs(configured=6), 6)
        self.assertEqual(resolve_max_active_jobs(env_raw="12", configured=6), 12)
        self.assertEqual(resolve_max_active_jobs(env_raw="bad", configured=6), 6)
        self.assertEqual(resolve_max_active_jobs(env_raw=None, configured=None), 10)

    def test_resolve_effective_max_active_jobs_clamps_pocket(self) -> None:
        self.assertEqual(
            resolve_effective_max_active_jobs(
                PocketTtsConfig(provider="pocket", model="default", pythonCommand=None),
                configured=6,
            ),
            1,
        )

    def test_resolve_effective_max_active_jobs_uses_normal_logic_for_inworld(self) -> None:
        self.assertEqual(
            resolve_effective_max_active_jobs(
                InworldTtsConfig(provider="inworld", apiKey="iw-key", model=None),
                configured=6,
            ),
            6,
        )

    def test_resolve_worker_server_type_accepts_publisher_variants(self) -> None:
        self.assertEqual(resolve_worker_server_type(None).name, "room")
        self.assertEqual(resolve_worker_server_type("publisher").name, "publisher")
        self.assertEqual(resolve_worker_server_type("JT_PUBLISHER").name, "publisher")
        self.assertEqual(resolve_worker_server_type("1").name, "publisher")
        self.assertEqual(resolve_worker_server_type("bad-value").name, "room")

    def test_room_metadata_parsing_and_call_id_fallback(self) -> None:
        ctx = SimpleNamespace(
            job=SimpleNamespace(
                room=SimpleNamespace(
                    metadata='{"callId":"call-abc","personId":"person-abc","audience":"owner","direction":"outbound","channel":"phone","modality":"voice","systemPrompt":"hello","voiceId":"Mark","firstMessage":"Hi"}',
                    name="call-xyz",
                )
            )
        )
        metadata = parse_room_metadata(ctx)
        self.assertEqual(metadata.call_id, "call-abc")
        self.assertEqual(metadata.person_id, "person-abc")
        self.assertEqual(metadata.audience, "owner")
        self.assertEqual(metadata.direction, "outbound")
        self.assertEqual(metadata.channel, "phone")
        self.assertEqual(metadata.modality, "voice")
        self.assertFalse(metadata.bootstrap)
        self.assertEqual(resolve_call_id(ctx, metadata), "call-abc")

        fallback_ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(metadata="{bad", name="call-fallback"))
        )
        fallback_metadata = parse_room_metadata(fallback_ctx)
        self.assertIsNone(fallback_metadata.call_id)
        self.assertEqual(resolve_call_id(fallback_ctx, fallback_metadata), "fallback")

    def test_room_metadata_kind_defaults_to_dashboard_and_parses_game(self) -> None:
        import json

        default_ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(
                metadata=json.dumps({"callId": "c-1", "personId": "p-1", "channel": "dashboard", "modality": "voice"}),
                name="r",
            ))
        )
        self.assertEqual(parse_room_metadata(default_ctx).kind, "dashboard")

        game_ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(
                metadata=json.dumps({"kind": "game", "channel": "dashboard", "modality": "voice"}),
                name="game-alpha-abcd",
            ))
        )
        self.assertEqual(parse_room_metadata(game_ctx).kind, "game")

        bogus_ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(
                metadata=json.dumps({"kind": "phone"}),
                name="r",
            ))
        )
        self.assertEqual(parse_room_metadata(bogus_ctx).kind, "dashboard")

    def test_room_metadata_parses_bootstrap_without_first_message(self) -> None:
        import json

        payload = json.dumps({
            "callId": "call-bootstrap",
            "personId": "person-bootstrap",
            "audience": "owner",
            "direction": "inbound",
            "systemPrompt": "bootstrap prompt",
            "bootstrap": True,
        })
        ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(metadata=payload, name="call-bootstrap"))
        )
        metadata = parse_room_metadata(ctx)
        self.assertTrue(metadata.bootstrap)
        self.assertIsNone(metadata.first_message)

    def test_room_metadata_parses_attention_cue_and_timeout(self) -> None:
        import json

        payload = json.dumps({
            "callId": "call-esc",
            "personId": "person-esc",
            "audience": "owner",
            "direction": "outbound",
            "systemPrompt": "prompt",
            "voiceId": "Mark",
            "firstMessage": "Escalation: call landlord",
            "attentionCue": True,
            "noResponseTimeout": 20,
        })
        ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(metadata=payload, name="call-esc"))
        )
        metadata = parse_room_metadata(ctx)
        self.assertTrue(metadata.attention_cue)
        self.assertEqual(metadata.no_response_timeout, 20.0)

    def test_room_metadata_defaults_attention_cue_false(self) -> None:
        import json

        payload = json.dumps({"callId": "call-basic", "audience": "owner", "direction": "inbound"})
        ctx = SimpleNamespace(
            job=SimpleNamespace(room=SimpleNamespace(metadata=payload, name="call-basic"))
        )
        metadata = parse_room_metadata(ctx)
        self.assertFalse(metadata.attention_cue)
        self.assertFalse(metadata.bootstrap)
        self.assertIsNone(metadata.no_response_timeout)

    def test_room_metadata_rejects_non_positive_timeout(self) -> None:
        import json

        for bad_val in [0, -5, "abc", None]:
            payload = json.dumps({"callId": "call-bad", "noResponseTimeout": bad_val})
            ctx = SimpleNamespace(
                job=SimpleNamespace(room=SimpleNamespace(metadata=payload, name="call-bad"))
            )
            metadata = parse_room_metadata(ctx)
            self.assertIsNone(metadata.no_response_timeout, f"Expected None for {bad_val!r}")


class PocketVoiceHelperTests(unittest.TestCase):
    def test_pocket_onnx_download_base_is_pinned_to_snapshot(self) -> None:
        self.assertIn("/resolve/", POCKET_ONNX_BASE)
        self.assertNotIn("/resolve/main", POCKET_ONNX_BASE)

    def test_resolve_pocket_voice_maps_default_to_hades_clip(self) -> None:
        resolved = _resolve_pocket_voice(None)
        self.assertTrue(resolved.endswith("voices/hades.wav"))
        self.assertTrue(Path(resolved).exists())
        self.assertEqual(_resolve_pocket_voice(""), resolved)
        self.assertEqual(_resolve_pocket_voice("default"), resolved)

    def test_resolve_pocket_voice_maps_named_clip_and_falls_back_for_unknown(self) -> None:
        resolved = _resolve_pocket_voice("Mark")
        self.assertTrue(resolved.endswith("voices/mark.wav"))
        self.assertTrue(Path(resolved).exists())
        self.assertEqual(_resolve_pocket_voice("custom-voice.wav"), "custom-voice.wav")

    def test_to_pcm16_bytes_converts_float_audio_without_torch(self) -> None:
        pcm = _to_pcm16_bytes(np.array([-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5], dtype=np.float32))
        decoded = np.frombuffer(pcm, dtype=np.int16).tolist()
        self.assertEqual(decoded, [-32767, -32767, -16384, 0, 16384, 32767, 32767])

    def test_load_pocket_engine_fails_fast_when_models_are_missing(self) -> None:
        with (
            patch("mystic.voice.pocket_onnx_models_missing", return_value=["tokenizer.model"]),
            patch("mystic.voice._import_pocket_tts_onnx_class") as import_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "Pocket ONNX models are missing"):
                _load_pocket_engine("default")
        import_mock.assert_not_called()
