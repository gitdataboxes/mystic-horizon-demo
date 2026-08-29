"""Runtime startup and shutdown orchestration."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mystic.actions import drain_scheduler, start_scheduler
from mystic.worker import WorkerConfig, start_agent_worker, stop_agent_worker
from mystic.calls import (
    drain_pending_bridge_tasks,
    drain_pending_extraction_tasks,
    get_default_voice_id,
    handle_unanswered_outbound_by_call_id,
    set_extraction_pipeline,
    sweep_timed_out_calls,
)
from mystic.config import (
    get_agent_config,
    get_providers_config,
    get_error_message,
    logger,
    set_tunnel_url,
)
from mystic.db import prune_ended_active_calls, close_database, initialize_schema, open_database, run_migrations
from mystic.http import stop_tunnel
from mystic.memory import (
    drain_nightly_loop,
    index_faq_files,
    start_nightly_loop,
)
from mystic.phone import PhoneReadiness, ensure_phone_line_ready
from mystic.livekit import ensure_livekit_binary, start_livekit_server, stop_livekit_server, wait_for_livekit_server
from mystic.server import create_app, start_server
from mystic.skills import init_skills

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
SWEEP_INTERVAL_MS = 60_000
PHONE_RECONCILE_INTERVAL_MS = 60_000


@dataclass
class Runtime:
    db: sqlite3.Connection | None
    server: object | None
    livekit_proc: subprocess.Popen[bytes] | None
    tunnel_url: str
    port: int
    _sweep_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _phone_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stopped: bool = field(default=False, repr=False)


async def start_full() -> Runtime:
    db = open_database()
    return await _start_runtime(db, owns_db=True)


async def start_dev(*, skip_voice: bool = False) -> Runtime:
    """Lightweight startup for development: DB + optional LiveKit + server."""
    agent_config = get_agent_config()
    providers_config = get_providers_config()
    port = agent_config.server.port

    livekit_proc: subprocess.Popen[bytes] | None = None
    db: sqlite3.Connection | None = None
    server: object | None = None

    try:
        if not skip_voice:
            await ensure_livekit_binary()
            livekit_proc = start_livekit_server(providers_config.livekit)
            await wait_for_livekit_server(providers_config.livekit, livekit_proc, timeout_ms=10_000)

        db = open_database()
        initialize_schema(db)
        run_migrations(db)

        init_skills(SKILLS_DIR)

        tunnel_url = f"http://localhost:{port}"
        set_tunnel_url(tunnel_url)

        app = create_app(db, tunnel_url)
        app["dev_mode"] = True
        server = await start_server(app, port)

        return Runtime(
            db=db, server=server, livekit_proc=livekit_proc,
            tunnel_url=tunnel_url, port=port,
        )
    except Exception:
        await _rollback(db=db, server=server, livekit_proc=livekit_proc, sweep_task=None, phone_task=None, tunnel_url=None)
        raise


async def start_runtime_from_setup(
    db: sqlite3.Connection,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    before_server: Callable[[], Awaitable[None]] | None = None,
) -> Runtime:
    """Start runtime from the setup flow using the existing database handle.

    ``before_server`` runs immediately before binding the HTTP port so the
    temporary setup server can release it at the last possible moment.
    """
    return await _start_runtime(
        db,
        owns_db=False,
        on_progress=on_progress,
        before_server=before_server,
    )


async def _start_runtime(
    db: sqlite3.Connection,
    *,
    owns_db: bool,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    before_server: Callable[[], Awaitable[None]] | None = None,
) -> Runtime:
    agent_config = get_agent_config()
    providers_config = get_providers_config()
    has_twilio = providers_config.twilio is not None
    port = agent_config.server.port

    livekit_proc: subprocess.Popen[bytes] | None = None
    server: object | None = None
    sweep_task: asyncio.Task[None] | None = None
    phone_task: asyncio.Task[None] | None = None
    tunnel_url: str | None = None

    async def _step(label: str) -> None:
        if on_progress is not None:
            await on_progress(label)

    try:
        await _step("Starting voice server...")
        await ensure_livekit_binary()
        livekit_proc = start_livekit_server(providers_config.livekit)
        await wait_for_livekit_server(providers_config.livekit, livekit_proc, timeout_ms=10_000)

        await _step("Initializing database...")
        initialize_schema(db)
        run_migrations(db)
        prune_ended_active_calls(db)

        await _step("Loading skills...")
        init_skills(SKILLS_DIR)

        await _step("Starting agent worker...")
        voice_id = agent_config.agent.voiceId or get_default_voice_id(providers_config.tts)
        await start_agent_worker(WorkerConfig(
            livekit_config=providers_config.livekit,
            tts_config=providers_config.tts,
            default_voice_id=voice_id,
            max_active_jobs=agent_config.server.maxActiveJobs,
        ))

        tunnel_url = f"http://localhost:{port}"
        set_tunnel_url(tunnel_url)

        await _step("Starting server...")
        if before_server is not None:
            await before_server()

        app = create_app(db, tunnel_url)
        server = await start_server(app, port)

        if has_twilio:
            await _step("Checking phone line...")
            try:
                readiness = await ensure_phone_line_ready(port=port, repair=True)
                if readiness.funnel.status == "ok" and readiness.public_url:
                    tunnel_url = readiness.public_url
                if readiness.status != "ok":
                    logger.warn("start.phone.degraded", status=readiness.status, reason=readiness.reason())
            except Exception as exc:
                logger.warn("start.phone.error", error=get_error_message(exc))
            phone_task = _start_phone_reconcile_loop(port)

        start_scheduler(db, tunnel_url)
        sweep_task = _start_sweep_loop(db)

        asyncio.create_task(_index_faq_background(db), name="faq-index")

        start_nightly_loop(db)

        return Runtime(
            db=db,
            server=server,
            livekit_proc=livekit_proc,
            tunnel_url=tunnel_url,
            port=port,
            _sweep_task=sweep_task,
            _phone_task=phone_task,
        )
    except Exception:
        await _rollback(
            db=db if owns_db else None,
            server=server,
            livekit_proc=livekit_proc,
            sweep_task=sweep_task,
            phone_task=phone_task,
            tunnel_url=tunnel_url,
        )
        raise


async def stop(rt: Runtime, *, drain_ms: int | None = None) -> None:
    if rt._stop_task is not None:
        await rt._stop_task
        return
    if rt._stopped:
        return

    async def _do_stop() -> None:
        rt._stopped = True
        effective_drain_ms = drain_ms if drain_ms is not None else 2_000

        try:
            await drain_scheduler(effective_drain_ms)
        except Exception:
            pass

        sweep_task = rt._sweep_task
        rt._sweep_task = None
        if sweep_task is not None:
            sweep_task.cancel()
            try:
                await sweep_task
            except (asyncio.CancelledError, Exception):
                pass

        phone_task = rt._phone_task
        rt._phone_task = None
        if phone_task is not None:
            phone_task.cancel()
            try:
                await phone_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await drain_nightly_loop(effective_drain_ms)
        except Exception:
            pass

        try:
            await stop_agent_worker()
        except Exception:
            pass

        try:
            await _close_server(rt.server)
        except Exception:
            pass
        rt.server = None

        try:
            stop_tunnel()
        except Exception:
            pass
        rt.tunnel_url = ""

        try:
            await drain_pending_extraction_tasks(effective_drain_ms)
        except Exception:
            pass
        try:
            await drain_pending_bridge_tasks(effective_drain_ms)
        except Exception:
            pass
        set_extraction_pipeline(None)

        if rt.livekit_proc is not None:
            try:
                stop_livekit_server(rt.livekit_proc)
            except Exception:
                pass
            rt.livekit_proc = None

        if rt.db is not None:
            try:
                close_database(rt.db)
            except Exception:
                pass
        rt.db = None

    rt._stop_task = asyncio.create_task(_do_stop(), name="runtime-stop")
    try:
        await asyncio.wait_for(asyncio.shield(rt._stop_task), timeout=12)
    except TimeoutError:
        logger.warn("runtime.stop.timeout")
    finally:
        rt._stop_task = None


async def _index_faq_background(db: sqlite3.Connection) -> None:
    try:
        await index_faq_files(db)
    except Exception as exc:
        logger.warn("start.faq.error", error=get_error_message(exc))


def _run_sweep(db: sqlite3.Connection) -> None:
    for call in sweep_timed_out_calls(db):
        call_id = _extract_swept_call_id(call)
        if isinstance(call_id, str) and call_id:
            handle_unanswered_outbound_by_call_id(db, call_id)


def _start_sweep_loop(db: sqlite3.Connection) -> asyncio.Task[None]:
    async def _runner() -> None:
        try:
            while True:
                await asyncio.sleep(SWEEP_INTERVAL_MS / 1000)
                _run_sweep(db)
        except asyncio.CancelledError:
            raise

    return asyncio.create_task(_runner(), name="runtime-sweep")


def _start_phone_reconcile_loop(port: int) -> asyncio.Task[None]:
    async def _runner() -> None:
        try:
            while True:
                await asyncio.sleep(PHONE_RECONCILE_INTERVAL_MS / 1000)
                providers = get_providers_config()
                if providers.twilio is None:
                    return
                try:
                    readiness: PhoneReadiness = await ensure_phone_line_ready(port=port, repair=True)
                    if readiness.status != "ok":
                        logger.warn("phone.reconcile.degraded", status=readiness.status, reason=readiness.reason())
                except Exception as exc:
                    logger.warn("phone.reconcile.error", error=get_error_message(exc))
        except asyncio.CancelledError:
            raise

    return asyncio.create_task(_runner(), name="phone-reconcile")


async def _rollback(
    *,
    db: sqlite3.Connection | None,
    server: object | None,
    livekit_proc: subprocess.Popen[bytes] | None,
    sweep_task: asyncio.Task[None] | None,
    phone_task: asyncio.Task[None] | None,
    tunnel_url: str | None,
) -> None:
    try:
        if sweep_task is not None:
            sweep_task.cancel()
            try:
                await sweep_task
            except (asyncio.CancelledError, Exception):
                pass

        if phone_task is not None:
            phone_task.cancel()
            try:
                await phone_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await stop_agent_worker()
        except Exception:
            pass

        await _close_server(server)

        if tunnel_url is not None:
            try:
                stop_tunnel()
            except Exception:
                pass

        try:
            await drain_pending_extraction_tasks(0)
        except Exception:
            pass
        try:
            await drain_pending_bridge_tasks(0)
        except Exception:
            pass
        try:
            await drain_scheduler(0)
        except Exception:
            pass
        try:
            await drain_nightly_loop(0)
        except Exception:
            pass
        set_extraction_pipeline(None)

        if livekit_proc is not None:
            try:
                stop_livekit_server(livekit_proc)
            except Exception:
                pass

        if db is not None:
            try:
                close_database(db)
            except Exception:
                pass
    except Exception as exc:
        logger.warn("runtime.rollback.error", error=get_error_message(exc))


async def _close_server(server: object | None) -> None:
    if server is None:
        return
    try:
        close = getattr(server, "close")
    except AttributeError:
        return

    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except TypeError:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def _done(*_args: object) -> None:
            if not future.done():
                future.set_result(None)

        close(_done)
        await asyncio.wait_for(future, timeout=3)
    except Exception:
        pass


def _extract_swept_call_id(call: object) -> str | None:
    direct = getattr(call, "call_id", None) or getattr(call, "callId", None)
    if isinstance(direct, str) and direct:
        return direct
    if not isinstance(call, Mapping):
        return None
    payload = cast(Mapping[str, object], call)
    value = payload.get("call_id") or payload.get("callId")
    return value if isinstance(value, str) and value else None
