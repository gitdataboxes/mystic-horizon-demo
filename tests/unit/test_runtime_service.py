from __future__ import annotations

import asyncio
import sqlite3
import unittest
from contextlib import ExitStack
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from mystic.config import (
    AgentConfig,
    AgentDetailsConfig,
    AgentHoursConfig,
    AgentOwnerConfig,
    AgentServerConfig,
    AgentTunnelConfig,
    LocalEmbeddingConfig,
    LiveKitConfig,
    MoonshineSttConfig,
    OpenRouterConfig,
    PocketTtsConfig,
    ProvidersConfig,
    TwilioConfig,
)
from mystic.phone import CapabilityReadiness, PhoneReadiness
from mystic.runtime import _run_sweep, start_full, start_runtime_from_setup, stop

NO_TWILIO_PROVIDERS = ProvidersConfig(
    twilio=None,
    livekit=LiveKitConfig(host="127.0.0.1", port=7880, apiKey="API123", apiSecret="secret"),
    stt=MoonshineSttConfig(provider="moonshine", model="small"),
    tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
    embedding=LocalEmbeddingConfig(provider="local", model="nomic-embed-text-v1.5", dimensions=256),
    openrouter=OpenRouterConfig(apiKey="openrouter-key"),
)

AGENT_CONFIG = AgentConfig(
    owner=AgentOwnerConfig(phone="+15551234567"),
    agent=AgentDetailsConfig(name="Mystic", voiceId="voice-primary"),
    hours=AgentHoursConfig(start=9, end=17, timezone="America/Los_Angeles", days=["monday"]),
    server=AgentServerConfig(port=3000, maxActiveJobs=3),
    tunnel=AgentTunnelConfig(enabled=True),
)

PROVIDERS_CONFIG = ProvidersConfig(
    twilio=TwilioConfig(accountSid="AC123", authToken="auth", phoneNumber="+14155550123", phoneNumberSid="PN123"),
    livekit=LiveKitConfig(host="127.0.0.1", port=7880, apiKey="API123", apiSecret="secret"),
    stt=MoonshineSttConfig(provider="moonshine", model="small"),
    tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
    embedding=LocalEmbeddingConfig(provider="local", model="nomic-embed-text-v1.5", dimensions=256),
    openrouter=OpenRouterConfig(apiKey="openrouter-key"),
)

DB = cast(sqlite3.Connection, object())
PROC = cast(Any, object())
PHONE_READY = PhoneReadiness(
    status="ok",
    public_url="https://test-machine.tail1234.ts.net",
    phone_number="+14155550123",
    phone_number_sid="PN123",
    tailscale=CapabilityReadiness("ok"),
    funnel=CapabilityReadiness("ok"),
    twilio=CapabilityReadiness("ok"),
)


class FakeServer:
    def __init__(self) -> None:
        self.close = AsyncMock()


def _runtime_patches(stack: ExitStack, overrides: dict) -> dict:
    """Enter all standard runtime patches, return dict of mock objects."""
    def p(target: str, **kw) -> object:
        m = stack.enter_context(patch(target, **kw))
        return m

    mocks: dict[str, object] = {}
    mocks["get_agent_config"] = p("mystic.runtime.get_agent_config", return_value=AGENT_CONFIG)
    mocks["get_providers_config"] = p("mystic.runtime.get_providers_config", return_value=PROVIDERS_CONFIG)
    mocks["ensure_livekit_binary"] = p("mystic.runtime.ensure_livekit_binary", new_callable=AsyncMock)
    mocks["start_livekit_server"] = p("mystic.runtime.start_livekit_server", return_value=PROC)
    mocks["wait_for_livekit_server"] = p("mystic.runtime.wait_for_livekit_server", new_callable=AsyncMock)
    mocks["open_database"] = p("mystic.runtime.open_database", return_value=DB)
    mocks["initialize_schema"] = p("mystic.runtime.initialize_schema")
    mocks["run_migrations"] = p("mystic.runtime.run_migrations", return_value=0)
    mocks["prune_ended_active_calls"] = p("mystic.runtime.prune_ended_active_calls")
    mocks["set_extraction_pipeline"] = p("mystic.runtime.set_extraction_pipeline")
    mocks["init_skills"] = p("mystic.runtime.init_skills")
    mocks["index_faq_files"] = p("mystic.runtime.index_faq_files", new_callable=AsyncMock, return_value=0)
    mocks["get_default_voice_id"] = p("mystic.runtime.get_default_voice_id", return_value="voice-default")
    mocks["start_agent_worker"] = p("mystic.runtime.start_agent_worker", new_callable=AsyncMock)
    mocks["ensure_phone_line_ready"] = p("mystic.runtime.ensure_phone_line_ready", new_callable=AsyncMock, return_value=PHONE_READY)
    mocks["create_app"] = p("mystic.runtime.create_app", return_value=object())
    mocks["start_server"] = p("mystic.runtime.start_server", new_callable=AsyncMock)
    mocks["_start_phone_reconcile_loop"] = p("mystic.runtime._start_phone_reconcile_loop")
    mocks["start_scheduler"] = p("mystic.runtime.start_scheduler")
    mocks["_start_sweep_loop"] = p("mystic.runtime._start_sweep_loop")
    mocks["start_nightly_loop"] = p("mystic.runtime.start_nightly_loop")
    mocks["drain_scheduler"] = p("mystic.runtime.drain_scheduler", new_callable=AsyncMock)
    mocks["stop_agent_worker"] = p("mystic.runtime.stop_agent_worker", new_callable=AsyncMock)
    mocks["stop_tunnel"] = p("mystic.runtime.stop_tunnel")
    mocks["drain_nightly_loop"] = p("mystic.runtime.drain_nightly_loop", new_callable=AsyncMock)
    mocks["drain_pending_extraction_tasks"] = p("mystic.runtime.drain_pending_extraction_tasks", new_callable=AsyncMock)
    mocks["drain_pending_bridge_tasks"] = p("mystic.runtime.drain_pending_bridge_tasks", new_callable=AsyncMock)
    mocks["stop_livekit_server"] = p("mystic.runtime.stop_livekit_server")
    mocks["close_database"] = p("mystic.runtime.close_database")

    for key, value in overrides.items():
        mocks[key] = stack.enter_context(patch(f"mystic.runtime.{key}", new=value))

    return mocks


class RuntimeStartFullTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_full_runtime_and_stops_cleanly(self) -> None:
        server = FakeServer()
        shutdown_events: list[str] = []

        async def fake_sweep_loop() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        with ExitStack() as stack:
            sweep_task = asyncio.create_task(fake_sweep_loop())
            mocks = _runtime_patches(stack, {
                "start_server": AsyncMock(return_value=server),
                "_start_sweep_loop": Mock(return_value=sweep_task),
            })
            mocks["stop_tunnel"].side_effect = lambda: shutdown_events.append("tunnel.stop")
            server.close.side_effect = lambda: shutdown_events.append("server.close")

            rt = await start_full()

            self.assertIs(rt.db, DB)
            self.assertIs(rt.server, server)
            self.assertIs(rt.livekit_proc, PROC)
            self.assertEqual(rt.tunnel_url, "https://test-machine.tail1234.ts.net")
            self.assertEqual(rt.port, AGENT_CONFIG.server.port)

            mocks["_start_sweep_loop"].assert_called_once_with(DB)
            mocks["ensure_phone_line_ready"].assert_awaited_once_with(port=AGENT_CONFIG.server.port, repair=True)
            mocks["_start_phone_reconcile_loop"].assert_called_once_with(AGENT_CONFIG.server.port)

            await stop(rt)
            await stop(rt)  # second call is noop

            mocks["drain_scheduler"].assert_awaited_once_with(2000)
            self.assertTrue(sweep_task.cancelled() or sweep_task.done())
            mocks["drain_nightly_loop"].assert_awaited_once_with(2000)
            mocks["stop_agent_worker"].assert_awaited_once()
            mocks["stop_tunnel"].assert_called_once_with()
            server.close.assert_awaited_once()
            self.assertEqual(shutdown_events, ["server.close", "tunnel.stop"])
            mocks["drain_pending_extraction_tasks"].assert_awaited_once_with(2000)
            mocks["set_extraction_pipeline"].assert_any_call(None)
            mocks["stop_livekit_server"].assert_called_once_with(PROC)
            mocks["close_database"].assert_called_once_with(DB)

    async def test_rolls_back_partial_full_startup_when_agent_worker_fails(self) -> None:
        with ExitStack() as stack:
            mocks = _runtime_patches(stack, {
                "start_agent_worker": AsyncMock(side_effect=RuntimeError("worker failed")),
            })

            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                await start_full()

            mocks["stop_tunnel"].assert_not_called()
            mocks["drain_scheduler"].assert_awaited_once_with(0)
            mocks["drain_nightly_loop"].assert_awaited_once_with(0)
            mocks["drain_pending_extraction_tasks"].assert_awaited_once_with(0)
            mocks["set_extraction_pipeline"].assert_any_call(None)
            mocks["stop_livekit_server"].assert_called_once_with(PROC)
            mocks["close_database"].assert_called_once_with(DB)

    async def test_start_runtime_from_setup_reuses_db_reports_progress_and_enters_before_server_hook(self) -> None:
        server = FakeServer()
        steps: list[str] = []

        async def fake_sweep_loop() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        async def on_progress(label: str) -> None:
            steps.append(label)

        before_server = AsyncMock()

        with ExitStack() as stack:
            sweep_task = asyncio.create_task(fake_sweep_loop())
            mocks = _runtime_patches(stack, {
                "start_server": AsyncMock(return_value=server),
                "_start_sweep_loop": Mock(return_value=sweep_task),
            })

            rt = await start_runtime_from_setup(DB, on_progress=on_progress, before_server=before_server)

            self.assertIs(rt.db, DB)
            mocks["open_database"].assert_not_called()
            before_server.assert_awaited_once_with()
            self.assertEqual(
                steps,
                [
                    "Starting voice server...",
                    "Initializing database...",
                    "Loading skills...",
                    "Starting agent worker...",
                    "Starting server...",
                    "Checking phone line...",
                ],
            )

            await stop(rt)

            mocks["close_database"].assert_called_once_with(DB)

    async def test_start_runtime_from_setup_keeps_setup_db_open_on_start_failure(self) -> None:
        with ExitStack() as stack:
            mocks = _runtime_patches(stack, {
                "start_agent_worker": AsyncMock(side_effect=RuntimeError("worker failed")),
            })

            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                await start_runtime_from_setup(DB)

            mocks["close_database"].assert_not_called()
            mocks["start_server"].assert_not_called()


class RunSweepTests(unittest.TestCase):
    def test_run_sweep_calls_handle_unanswered_for_timed_out_calls(self) -> None:
        db = object()
        call_records = [{"callId": "call-timeout-1"}, {"callId": "call-timeout-2"}]

        with (
            patch("mystic.runtime.sweep_timed_out_calls", return_value=call_records),
            patch("mystic.runtime.handle_unanswered_outbound_by_call_id") as m_handle,
        ):
            _run_sweep(db)  # type: ignore[arg-type]

        self.assertEqual(m_handle.call_count, 2)
        m_handle.assert_any_call(db, "call-timeout-1")
        m_handle.assert_any_call(db, "call-timeout-2")

    def test_run_sweep_skips_records_without_call_id(self) -> None:
        db = object()

        with (
            patch("mystic.runtime.sweep_timed_out_calls", return_value=[{"other": "field"}]),
            patch("mystic.runtime.handle_unanswered_outbound_by_call_id") as m_handle,
        ):
            _run_sweep(db)  # type: ignore[arg-type]

        m_handle.assert_not_called()


class RuntimeLocalModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_twilio_starts_scheduler_and_sweep(self) -> None:
        server = FakeServer()

        async def fake_sweep_loop() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        with ExitStack() as stack:
            sweep_task = asyncio.create_task(fake_sweep_loop())
            mocks = _runtime_patches(stack, {
                "get_providers_config": Mock(return_value=NO_TWILIO_PROVIDERS),
                "start_server": AsyncMock(return_value=server),
                "_start_sweep_loop": Mock(return_value=sweep_task),
            })

            rt = await start_full()

            mocks["start_scheduler"].assert_called_once_with(DB, f"http://localhost:{AGENT_CONFIG.server.port}")
            mocks["_start_sweep_loop"].assert_called_once_with(DB)
            mocks["ensure_phone_line_ready"].assert_not_awaited()
            mocks["_start_phone_reconcile_loop"].assert_not_called()

            await stop(rt)

            mocks["drain_pending_extraction_tasks"].assert_awaited_once()
            mocks["drain_pending_bridge_tasks"].assert_awaited_once()
