"""Dashboard surface: auth, rendering, fragments, chat, and SSE."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import importlib
import inspect
import json
import mimetypes
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web

from mystic.actions import cancel_action, complete_action
from mystic.config import (
    _serialize_config,
    ensure_dashboard_token,
    get_agent_config,
    get_dashboard_dir,
    get_dashboard_manifest_path,
    get_dashboard_pages_dir,
    get_dashboard_style_path,
    get_error_message,
    get_home,
    get_intelligence_config,
    get_providers_config,
    get_realtime_llm_config,
    identity_exists,
    is_python_package_available,
    list_journal_entries,
    soul_exists,
    get_tunnel_url,
    list_dashboard_files,
    read_identity,
    read_dashboard_file,
    register_event_listener,
    write_config,
    write_dashboard_file,
    logger,
)
from mystic.db import (
    count_active_calls,
    get_action_by_id,
    get_actions_by_status,
    get_all_active_facts_by_person,
    get_all_pending_actions,
    get_all_people,
    get_call_by_id,
    get_external_event_by_id,
    get_person_by_id,
    get_recent_calls,
    get_recent_calls_by_person,
    get_recent_external_events,
    insert_call,
    insert_game_score,
    list_active_calls,
    previous_best_game_score,
    rank_for_score,
    search_facts,
    get_call_transcript,
    top_game_scores,
    upsert_person,
)
from mystic.embedding import embedding_model_missing
from mystic.interactions import describe_call, describe_interaction, format_interaction_brief, interaction_event_payload
from mystic.livekit import create_named_room, delete_room, generate_token, parse_transcript_entries, room_has_active_agent, verify_dispatch_assignment
from mystic.llm import stream_llm_with_tools
from mystic.prompts import render
from mystic.skills import build_tools_for_context, execute_tool, get_registry
from mystic.types import ActionStatus, CallState, SkillContext
from mystic.voice import pocket_onnx_models_missing

if TYPE_CHECKING:
    from mystic.cli import InitSelections
    from mystic.runtime import Runtime
    from mystic.server import RunningServer

SESSION_COOKIE = "mh_session"
_SESSION_LABEL = b"mh-session"
DEFAULT_DASHBOARD_PATH = "/dashboard/page/home"
_TEMPLATE_CACHE: dict[str, str] = {}
_SSE_CLIENTS: set[asyncio.Queue[Any]] = set()
_DASHBOARD_VOICE_SESSION_KEY = "dashboard.voice.session"
_DASHBOARD_VOICE_LOCK_KEY = "dashboard.voice.lock"
_DASHBOARD_VOICE_IDLE_SECONDS = 30
_DASHBOARD_VOICE_ASSIGNMENT_TIMEOUT_MS = 1_500
_DASHBOARD_CHAT_CALL_PREFIX = "dashboard-chat"
_prepare_task: asyncio.Task[None] | None = None
_setup_done: asyncio.Event | None = None
_setup_runtime: Runtime | None = None
_setup_db: sqlite3.Connection | None = None
_setup_server: RunningServer | None = None

SETUP_DEFAULT_LLM_MODEL = "openai/gpt-5.5"
SETUP_DEFAULT_LLM_URL = "http://localhost:1234/v1"
SETUP_DEFAULT_TTS_VOICE = "Olivia"
SETTINGS_VOICE_OPTIONS = ("Hades", "Mark", "Clive", "Olivia", "Orietta", "Pippa")


@dataclass(slots=True)
class DashboardVoiceSession:
    call_id: str
    room_name: str
    person_id: str
    date_key: str
    participant_count: int = 0
    idle_task: asyncio.Task[None] | None = None
    participant_names: set[str] = field(default_factory=set)


def set_setup_done_event(event: asyncio.Event | None) -> None:
    global _setup_done
    _setup_done = event


def set_setup_db(db: sqlite3.Connection | None) -> None:
    global _setup_db
    _setup_db = db


def set_setup_server(server: RunningServer | None) -> None:
    global _setup_server
    _setup_server = server


def set_setup_runtime(runtime: Runtime | None) -> None:
    global _setup_runtime
    _setup_runtime = runtime


def get_setup_runtime() -> Runtime | None:
    return _setup_runtime


def get_assets_dir() -> Path:
    return Path(__file__).resolve().parent / "_assets"


def get_templates_dir() -> Path:
    return get_assets_dir() / "templates"


def get_static_dir() -> Path:
    return get_assets_dir() / "static"


def get_soundfx_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "soundfx"


def get_dashboard_defaults_dir() -> Path:
    return get_assets_dir() / "dashboard" / "defaults"


def _get_dashboard_voice_lock(app: web.Application) -> asyncio.Lock:
    existing = app.get(_DASHBOARD_VOICE_LOCK_KEY)
    if isinstance(existing, asyncio.Lock):
        return existing
    lock = asyncio.Lock()
    app[_DASHBOARD_VOICE_LOCK_KEY] = lock
    return lock


def _slugify_dashboard_room_part(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed or "agent"


def _dashboard_voice_date_key() -> str:
    agent = get_agent_config()
    try:
        tz = ZoneInfo(agent.hours.timezone)
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")
    return datetime.now(tz).strftime("%Y-%m-%d")


def _dashboard_voice_room_name() -> str:
    agent = get_agent_config()
    return f"owner-{_slugify_dashboard_room_part(agent.agent.name)}-{_dashboard_voice_date_key()}"


def _dashboard_chat_call_id() -> str:
    return _DASHBOARD_CHAT_CALL_PREFIX


async def _create_dashboard_room_with_agent_dispatch(
    livekit_config: Any,
    room_name: str,
    metadata: Mapping[str, object],
) -> None:
    """Create a dashboard room and dispatch the agent worker.

    The browser participant cannot join until this request returns a token.
    Treat assignment verification as an early signal only; otherwise a slow
    worker can be deleted while it is legitimately waiting for the dashboard.
    """
    try:
        await create_named_room(
            livekit_config,
            room_name,
            metadata,
            max_participants=8,
        )
    except Exception as exc:
        if "already exists" not in get_error_message(exc).lower():
            raise
        with suppress(Exception):
            await delete_room(livekit_config, room_name)
        await create_named_room(
            livekit_config,
            room_name,
            metadata,
            max_participants=8,
        )

    try:
        assigned = await verify_dispatch_assignment(
            livekit_config,
            room_name,
            timeout_ms=_DASHBOARD_VOICE_ASSIGNMENT_TIMEOUT_MS,
            poll_ms=250,
        )
    except Exception as exc:
        logger.warn(
            "dashboard.voice.dispatch.verify_error",
            room=room_name,
            error=get_error_message(exc),
        )
        return

    if assigned:
        logger.info("dashboard.voice.dispatch.assigned", room=room_name)
    else:
        logger.warn(
            "dashboard.voice.dispatch.pending",
            room=room_name,
            waitMs=_DASHBOARD_VOICE_ASSIGNMENT_TIMEOUT_MS,
        )


def _ensure_chat_session_for_person(db: sqlite3.Connection, person_id: str) -> str:
    call_id = _dashboard_chat_call_id()
    if get_call_by_id(db, call_id) is None:
        insert_call(
            db,
            person_id=person_id,
            direction="inbound",
            channel="dashboard",
            modality="text",
            audience="owner",
            call_id=call_id,
        )
    return call_id


def _load_dashboard_chat_history(
    db: sqlite3.Connection,
    person_id: str,
) -> tuple[str, list[dict[str, object]]]:
    call_id = _ensure_chat_session_for_person(db, person_id)
    history = _load_call_history(db, call_id)
    return call_id, history


def _load_call_history(db: sqlite3.Connection, call_id: str) -> list[dict[str, object]]:
    transcript_text = get_call_transcript(db, call_id)
    return parse_transcript_entries(transcript_text) if transcript_text else []


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


async def acquire_dashboard_voice_session(
    app: web.Application,
    db: sqlite3.Connection,
    *,
    participant_name: str | None = None,
) -> DashboardVoiceSession:
    lock = _get_dashboard_voice_lock(app)
    async with lock:
        existing = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        if isinstance(existing, DashboardVoiceSession):
            if existing.idle_task is not None and not existing.idle_task.done():
                existing.idle_task.cancel()
            existing.idle_task = None

            if existing.date_key != _dashboard_voice_date_key():
                with suppress(Exception):
                    await delete_room(get_providers_config().livekit, existing.room_name)
                app[_DASHBOARD_VOICE_SESSION_KEY] = None
            elif _dashboard_voice_active_participants(existing) > 0:
                _register_dashboard_voice_participant(existing, participant_name)
                return existing
            else:
                await _refresh_dashboard_voice_room(db, existing)
                _register_dashboard_voice_participant(existing, participant_name)
                return existing

        created = await _create_dashboard_voice_session(db)
        _register_dashboard_voice_participant(created, participant_name)
        app[_DASHBOARD_VOICE_SESSION_KEY] = created
        return created


async def release_dashboard_voice_session(
    app: web.Application,
    session: DashboardVoiceSession,
    *,
    participant_name: str | None = None,
) -> None:
    lock = _get_dashboard_voice_lock(app)
    async with lock:
        current = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        if current is not session:
            return
        if participant_name:
            if participant_name not in session.participant_names:
                return
            session.participant_names.discard(participant_name)
            session.participant_count = len(session.participant_names)
        elif session.participant_names:
            return
        else:
            session.participant_count = max(0, session.participant_count - 1)

        if _dashboard_voice_active_participants(session) > 0:
            return
        if session.idle_task is None or session.idle_task.done():
            session.idle_task = asyncio.create_task(
                _expire_dashboard_voice_session(app, session),
                name=f"dashboard-voice-idle-{session.call_id}",
            )


def _dashboard_voice_active_participants(session: DashboardVoiceSession) -> int:
    if session.participant_names:
        return len(session.participant_names)
    return session.participant_count


def _register_dashboard_voice_participant(
    session: DashboardVoiceSession,
    participant_name: str | None,
) -> None:
    if participant_name:
        session.participant_names.add(participant_name)
        session.participant_count = len(session.participant_names)
        return
    session.participant_count += 1


async def _create_dashboard_voice_session(db: sqlite3.Connection) -> DashboardVoiceSession:
    from mystic.calls import (
        LOCAL_OWNER_PHONE,
        add_active_call,
        assemble_context,
        build_bootstrap_system_prompt,
        get_active_calls,
        get_default_voice_id,
    )

    agent = get_agent_config()
    providers = get_providers_config()
    owner_phone = agent.owner.phone or LOCAL_OWNER_PHONE
    owner = upsert_person(db, owner_phone, name="Owner")
    needs_bootstrap = not identity_exists() or not soul_exists()
    # Bootstrap sessions are text-first (audio disabled); use "text" modality
    # so the graph and activity feed label them correctly.
    session_modality: str = "text" if needs_bootstrap else "voice"
    system_prompt = (
        build_bootstrap_system_prompt()
        if needs_bootstrap
        else assemble_context(
            db,
            owner,
            "owner",
            "inbound",
            get_active_calls(db),
            get_tunnel_url() or f"http://localhost:{agent.server.port}",
            channel="dashboard",
            modality=session_modality,
        )
    )
    chat_call_id = _ensure_chat_session_for_person(db, owner.id)
    call = insert_call(
        db,
        person_id=owner.id,
        direction="inbound",
        channel="dashboard",
        modality=session_modality,
        audience="owner",
    )
    room_name = _dashboard_voice_room_name()
    metadata: dict[str, object] = {
        "callId": call.id,
        "personId": owner.id,
        "audience": "owner",
        "direction": "inbound",
        "channel": "dashboard",
        "modality": session_modality,
        "systemPrompt": system_prompt,
        "voiceId": agent.agent.voiceId or get_default_voice_id(providers.tts),
        "chatCallId": chat_call_id,
    }
    if needs_bootstrap:
        chat_call = get_call_by_id(db, chat_call_id)
        if chat_call is None or not (chat_call.transcript or "").strip():
            metadata["bootstrap"] = True

    await _create_dashboard_room_with_agent_dispatch(
        providers.livekit,
        room_name,
        metadata,
    )

    add_active_call(
        CallState(
            call_id=call.id,
            person_id=owner.id,
            person_name=owner.name,
            audience="owner",
            direction="inbound",
            channel="dashboard",
            modality=session_modality,
            started_at=call.started_at,
        ),
        db,
    )
    await broadcast(
        "activity",
        {
            "type": "live_voice_started",
            "call_id": call.id,
            "room": room_name,
            **interaction_event_payload(
                describe_interaction(
                    direction="inbound",
                    channel="dashboard",
                    modality=session_modality,
                )
            ),
        },
    )
    return DashboardVoiceSession(
        call_id=call.id,
        room_name=room_name,
        person_id=owner.id,
        date_key=_dashboard_voice_date_key(),
    )


async def _refresh_dashboard_voice_room(
    db: sqlite3.Connection,
    session: DashboardVoiceSession,
) -> None:
    providers = get_providers_config()

    # If the agent worker is still dispatched in the room, skip the
    # destructive delete+recreate.  This preserves the agent's in-memory
    # LLM conversation context across browser refreshes.
    if await room_has_active_agent(providers.livekit, session.room_name):
        return

    from mystic.calls import (
        LOCAL_OWNER_PHONE,
        assemble_context,
        build_bootstrap_system_prompt,
        get_active_calls,
        get_default_voice_id,
    )

    agent = get_agent_config()
    owner_phone = agent.owner.phone or LOCAL_OWNER_PHONE
    owner = upsert_person(db, owner_phone, name="Owner")
    needs_bootstrap = not identity_exists() or not soul_exists()
    session_modality: str = "text" if needs_bootstrap else "voice"
    system_prompt = (
        build_bootstrap_system_prompt()
        if needs_bootstrap
        else assemble_context(
            db,
            owner,
            "owner",
            "inbound",
            get_active_calls(db),
            get_tunnel_url() or f"http://localhost:{agent.server.port}",
            channel="dashboard",
            modality=session_modality,
        )
    )
    chat_call_id = _ensure_chat_session_for_person(db, session.person_id)
    metadata: dict[str, object] = {
        "callId": session.call_id,
        "personId": session.person_id,
        "audience": "owner",
        "direction": "inbound",
        "channel": "dashboard",
        "modality": session_modality,
        "systemPrompt": system_prompt,
        "voiceId": agent.agent.voiceId or get_default_voice_id(providers.tts),
        "chatCallId": chat_call_id,
    }
    if needs_bootstrap:
        chat_call = get_call_by_id(db, chat_call_id)
        if chat_call is None or not (chat_call.transcript or "").strip():
            metadata["bootstrap"] = True

    await _create_dashboard_room_with_agent_dispatch(
        providers.livekit,
        session.room_name,
        metadata,
    )


async def _expire_dashboard_voice_session(
    app: web.Application,
    session: DashboardVoiceSession,
) -> None:
    try:
        await asyncio.sleep(_DASHBOARD_VOICE_IDLE_SECONDS)
    except asyncio.CancelledError:
        return

    lock = _get_dashboard_voice_lock(app)
    async with lock:
        current = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        if current is not session or _dashboard_voice_active_participants(session) > 0:
            return
        session.idle_task = None

    with suppress(Exception):
        await delete_room(get_providers_config().livekit, session.room_name)
    await broadcast(
        "activity",
        {
            "type": "live_voice_stopped",
            "call_id": session.call_id,
            "room": session.room_name,
        },
    )


async def cleanup_dashboard_voice_session(app: web.Application) -> None:
    lock = _get_dashboard_voice_lock(app)
    async with lock:
        session = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        app[_DASHBOARD_VOICE_SESSION_KEY] = None
    if not isinstance(session, DashboardVoiceSession):
        return
    if session.idle_task is not None and not session.idle_task.done():
        session.idle_task.cancel()
        with suppress(asyncio.CancelledError):
            await session.idle_task
    with suppress(Exception):
        await delete_room(get_providers_config().livekit, session.room_name)


def seed_dashboard_defaults() -> list[Path]:
    defaults_dir = get_dashboard_defaults_dir()
    dashboard_dir = get_dashboard_dir()
    created: list[Path] = []
    if not defaults_dir.exists():
        return created

    for source in sorted(defaults_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(defaults_dir)
        destination = dashboard_dir / relative
        if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(destination)
    return created


def build_session_cookie(token: str) -> str:
    digest = hmac.new(token.encode("utf-8"), _SESSION_LABEL, hashlib.sha256).hexdigest()
    return digest


def is_authenticated(request: web.Request) -> bool:
    token = ensure_dashboard_token()
    expected = build_session_cookie(token)
    presented = request.cookies.get(SESSION_COOKIE, "")
    return bool(presented) and hmac.compare_digest(presented, expected)


def require_dashboard_auth(request: web.Request) -> web.Response | None:
    if is_authenticated(request):
        return None
    destination = quote(request.path_qs or "/dashboard")
    raise web.HTTPFound(f"/dashboard/login?next={destination}")


def render_dashboard_template(name: str, variables: Mapping[str, object] | None = None) -> str:
    template = _load_template(name)
    escaped = _escape_variables(variables or {})
    return render(template, escaped)


def render_setup_shell(content: str, *, title: str) -> str:
    shell = _load_template("setup-shell.html")
    shell = shell.replace("{{{content}}}", content)
    return render(shell, {"title": html.escape(title)})


def render_shell(content: str, *, title: str, current_path: str, hx_request: bool = False) -> str:
    if hx_request:
        return content
    agent = get_agent_config()
    pages = _dashboard_nav_items()
    nav_links = [p for p in pages if p["href"] != "/dashboard/page/home"]
    shell = _load_template("shell.html")
    shell = shell.replace("{{{content}}}", content)
    return render(
        shell,
        {
            "title": html.escape(title),
            "agent_name": html.escape(agent.agent.name),
            "nav": "".join(
                _nav_link(
                    item["href"],
                    item["label"],
                    current_path=current_path,
                    icon=_NAV_ICONS.get(item.get("slug", ""), _ICON_DEFAULT),
                )
                for item in nav_links
            ),
        },
    )


_ICON_CALENDAR = '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M8 2v4m8-4v4M3 10h18"/></svg>'
_ICON_PHONE = '<svg class="icon" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.13.96.36 1.9.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0122 16.92z"/></svg>'
_ICON_PERSON = '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a8 8 0 0116 0v1"/></svg>'
_ICON_DEFAULT = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 2l10 10-10 10L2 12z"/></svg>'

_NAV_ICONS: dict[str, str] = {
    "actions": _ICON_CALENDAR,
    "calls": _ICON_PHONE,
    "people": _ICON_PERSON,
}


def _nav_link(href: str, label: str, *, current_path: str, icon: str = _ICON_DEFAULT) -> str:
    class_name = "nav-link is-active" if href == current_path else "nav-link"
    aria_current = ' aria-current="page"' if href == current_path else ""
    return (
        f'<a class="{class_name}" href="{html.escape(href)}"{aria_current}'
        f' title="{html.escape(label)}">'
        f'<span class="nav-icon" aria-hidden="true">{icon}</span>'
        f'<span class="nav-label">{html.escape(label)}</span></a>'
    )


def _dashboard_nav_items() -> list[dict[str, str]]:
    pages_dir = get_dashboard_pages_dir()
    items: list[dict[str, str]] = []
    if not pages_dir.exists():
        return items
    for path in sorted(pages_dir.glob("*.html")):
        slug = path.stem
        label = slug.replace("-", " ").title()
        items.append({"href": f"/dashboard/page/{slug}", "label": label, "slug": slug})
    return items


def _load_template(name: str) -> str:
    cached = _TEMPLATE_CACHE.get(name)
    if cached is not None:
        return cached
    path = get_templates_dir() / name
    template = path.read_text(encoding="utf-8")
    _TEMPLATE_CACHE[name] = template
    return template


def _escape_variables(values: Mapping[str, object]) -> dict[str, object]:
    escaped: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            escaped[key] = value
        elif isinstance(value, (int, float)):
            escaped[key] = str(value)
        elif value is None:
            escaped[key] = ""
        else:
            escaped[key] = html.escape(str(value))
    return escaped


def _broadcast_sync(event: str, data: Mapping[str, object]) -> None:
    """Sync broadcast — used as emit_event listener and by the async wrapper."""
    payload = f"event: {event}\ndata: {json.dumps(dict(data))}\n\n"
    dead: list[asyncio.Queue[str]] = []
    for queue in list(_SSE_CLIENTS):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(queue)
    for queue in dead:
        _SSE_CLIENTS.discard(queue)


async def broadcast(event: str, data: Mapping[str, object]) -> None:
    _broadcast_sync(event, data)


def _wake_sse_clients_for_shutdown() -> None:
    for queue in list(_SSE_CLIENTS):
        while True:
            try:
                queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    continue
                break


async def _shutdown_dashboard_streams(_app: web.Application) -> None:
    _wake_sse_clients_for_shutdown()


async def _write_sse(response: web.StreamResponse, payload: str | bytes) -> bool:
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    try:
        await response.write(data)
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        return False
    return True


def _tool_args_summary(name: str, args: dict[str, object]) -> str:
    """One-line human-readable summary of tool call arguments."""
    query = str(
        args.get("query", "")
        or args.get("text", "")
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


def _render_call_row(call: object) -> str:
    descriptor = describe_call(call)
    call_id = str(getattr(call, "id", ""))
    audience = str(getattr(call, "audience", ""))
    started_at = str(getattr(call, "started_at", ""))
    return (
        "<tr>"
        f"<td>{html.escape(descriptor.label)}</td>"
        f"<td>{html.escape(descriptor.direction_label)}</td>"
        f"<td>{html.escape(audience)}</td>"
        f"<td>{html.escape(started_at)}</td>"
        f'<td><a href="/dashboard/f/call/{html.escape(call_id)}">Open</a></td>'
        "</tr>"
    )


async def run_owner_chat(
    db: sqlite3.Connection,
    message: str,
    *,
    history: Iterable[Mapping[str, object]] | None = None,
    source: str = "dashboard",
    call_id: str | None = None,
    on_text: Callable[[str], None | Awaitable[None]] | None = None,
    on_tool: Callable[..., None | Awaitable[None]] | None = None,
) -> str:
    if not message.strip():
        return "Please send a message."

    from mystic.calls import LOCAL_OWNER_PHONE, assemble_context, get_active_calls

    agent = get_agent_config()
    owner_phone = agent.owner.phone or LOCAL_OWNER_PHONE
    person = upsert_person(db, owner_phone, name="Owner")
    system_prompt = assemble_context(
        db,
        person,
        "owner",
        "inbound",
        get_active_calls(db),
        get_tunnel_url() or f"http://localhost:{agent.server.port}",
        channel="cli" if source == "cli" else "dashboard",
        modality="text",
    )
    messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    for turn in history or ():
        role = turn.get("role")
        content = turn.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    ctx = SkillContext(
        audience="owner",
        direction="inbound",
        channel="cli" if source == "cli" else "dashboard",
        modality="text",
        call_id=call_id or f"{source}-chat",
        person_id=person.id,
        source="owner",
    )
    config = get_realtime_llm_config()

    async def execute_with_events(name: str, arguments: dict[str, object]) -> str:
        summary = _tool_args_summary(name, arguments)
        if on_tool is not None:
            await _maybe_await(on_tool("tool_started", name, args_summary=summary))
        t0 = time.monotonic()
        error = False
        try:
            return await execute_tool(db, ctx, name, arguments)
        except Exception:
            error = True
            raise
        finally:
            elapsed = int((time.monotonic() - t0) * 1000)
            if on_tool is not None:
                await _maybe_await(on_tool(
                    "tool_completed", name,
                    duration_ms=elapsed, error=error,
                ))

    return await stream_llm_with_tools(
        messages,
        config,
        tools=build_tools_for_context(get_registry(), "owner", "text"),
        execute_fn=execute_with_events,
        on_text=on_text,
    )


def _prepare_in_progress() -> bool:
    return _prepare_task is not None and not _prepare_task.done()


def _prepare_fragment(message: str) -> str:
    return (
        '<p id="prepare-feedback" class="settings-setup-message" aria-live="polite">'
        f"{html.escape(message)}</p>"
    )


def _maybe_import_sibling_twilio_credentials() -> bool:
    providers = get_providers_config()
    if providers.twilio is not None or providers.twilioDraft is not None:
        return False
    from mystic.cli import discover_siblings, extract_sibling_keys
    siblings = discover_siblings(get_home().name)
    if not siblings:
        return False
    sid = ""
    token = ""
    for sibling in siblings:
        sibling_keys = extract_sibling_keys(sibling)
        sid = sibling_keys.get("twilioSid", "")
        token = sibling_keys.get("twilioToken", "")
        if sid and token:
            break
    else:
        return False
    payload = {**_serialize_config(providers)}
    payload.pop("twilio", None)
    payload["twilioDraft"] = {"accountSid": sid, "authToken": token}
    write_config("providers.json", payload)
    return True


def _setup_mode(provider: str | None, *, local_providers: set[str], default: str = "cloud") -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in local_providers:
        return "local"
    if normalized:
        return "cloud"
    return default


def _setup_llm_model() -> str:
    providers = get_providers_config()
    slot = providers.llm.realtime if providers.llm else None
    configured = str(getattr(slot, "model", "") or "").strip()
    if configured:
        return configured
    return SETUP_DEFAULT_LLM_MODEL


def _llm_setup_required() -> bool:
    providers = get_providers_config()
    slot = providers.llm.realtime if providers.llm else None
    return not str(getattr(slot, "provider", "") or "").strip()


def _setup_llm_url() -> str:
    providers = get_providers_config()
    slot = providers.llm.realtime if providers.llm else None
    configured = str(getattr(slot, "baseURL", "") or "").strip()
    return configured or SETUP_DEFAULT_LLM_URL


def _update_setup_intelligence_model(model: str) -> None:
    intelligence = get_intelligence_config()
    payload = _serialize_config(intelligence)

    for section, key in (
        ("extraction", "facts"),
        ("extraction", "commitments"),
        ("judgment", "scheduler"),
        ("judgment", "satisfaction"),
        ("judgment", "owner_call"),
        ("summarization", "person"),
        ("summarization", "call"),
    ):
        section_payload = payload.get(section)
        if isinstance(section_payload, dict):
            item_payload = section_payload.get(key)
            if isinstance(item_payload, dict):
                item_payload["model"] = model

    for key in ("editing", "search"):
        item_payload = payload.get(key)
        if isinstance(item_payload, dict):
            item_payload["model"] = model

    write_config("intelligence.json", payload)


def _moonshine_model_ready(model: str | None) -> bool:
    if not is_python_package_available("moonshine_voice"):
        return False

    try:
        moonshine_voice = importlib.import_module("moonshine_voice")
        moonshine_download = importlib.import_module("moonshine_voice.download")
        moonshine_download_file = importlib.import_module("moonshine_voice.download_file")
    except Exception:
        return False

    normalized = str(model or "small").strip().lower() or "small"
    arch_name = {
        "tiny": "TINY_STREAMING",
        "small": "SMALL_STREAMING",
        "medium": "MEDIUM_STREAMING",
    }.get(normalized)
    if arch_name is None:
        return False

    model_arch = getattr(moonshine_voice.ModelArch, arch_name, None)
    if model_arch is None:
        return False

    try:
        model_info = moonshine_download.find_model_info("en", model_arch)
        components = moonshine_download.get_components_for_model_info(model_info)
        root_model_path = Path(moonshine_download_file.get_cache_dir()) / str(
            model_info["download_url"]
        ).replace("https://", "")
    except Exception:
        return False

    return all((root_model_path / component).exists() for component in components)


def _voice_readiness() -> dict[str, object]:
    providers = get_providers_config()
    stt_provider = str(getattr(providers.stt, "provider", "") or "").strip().lower()
    tts_provider = str(getattr(providers.tts, "provider", "") or "").strip().lower()

    stt_ready = False
    if stt_provider == "moonshine":
        stt_ready = (
            is_python_package_available("moonshine_voice")
            and _moonshine_model_ready(getattr(providers.stt, "model", None))
        )
    elif stt_provider == "deepgram":
        stt_ready = bool(str(getattr(providers.stt, "apiKey", "") or "").strip())

    tts_ready = False
    if tts_provider == "pocket":
        tts_ready = not pocket_onnx_models_missing()
    elif tts_provider == "inworld":
        tts_ready = (
            bool(str(getattr(providers.tts, "apiKey", "") or "").strip())
            and is_python_package_available("livekit.plugins.inworld")
        )

    return {
        "stt_provider": stt_provider,
        "tts_provider": tts_provider,
        "stt_ready": stt_ready,
        "tts_ready": tts_ready,
        "embedding_ready": not embedding_model_missing(),
    }


def _is_hx_request(request: web.Request) -> bool:
    return bool(request.headers.get("HX-Request"))


def _dashboard_setup_warnings(request: web.Request) -> list[str]:
    dev_mode = request.app.get("dev_mode", False)
    warnings: list[str] = []
    if _llm_setup_required():
        if not dev_mode:
            raise web.HTTPFound("/dashboard/setup")
        warnings.append("LLM realtime provider not configured")
    readiness = _voice_readiness()
    if not readiness["stt_ready"] or not readiness["tts_ready"]:
        if not dev_mode:
            raise web.HTTPFound("/dashboard/setup")
        if not readiness["stt_ready"]:
            warnings.append(f"STT not ready (provider: {readiness['stt_provider'] or 'none'})")
        if not readiness["tts_ready"]:
            warnings.append(f"TTS not ready (provider: {readiness['tts_provider'] or 'none'})")
    return warnings


def _with_dashboard_warnings(content: str, warnings: Iterable[str]) -> str:
    items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
    if not items:
        return content
    banner = (
        '<div style="background:#2a1800;border:1px solid #b45309;'
        'border-radius:6px;padding:8px 12px;margin:0 0 12px;font-size:13px;color:#fbbf24;">'
        "<strong>Dev mode</strong> - some providers are not ready:"
        f'<ul style="margin:4px 0 0;padding-left:18px;">{items}</ul>'
        "</div>"
    )
    return banner + content


def _build_prepare_selections() -> "InitSelections":
    from mystic.cli import InitSelections

    agent = get_agent_config()
    providers = get_providers_config()
    payload = _serialize_config(providers)
    llm_payload = cast(dict[str, object], payload.get("llm") or {})
    return InitSelections(
        timezone=agent.hours.timezone,
        selected_voice_id=agent.agent.voiceId or "",
        server_port=agent.server.port,
        livekit_port=providers.livekit.port,
        tts_config=cast(dict[str, object], payload.get("tts") or {"provider": ""}),
        stt_config=cast(dict[str, object], payload.get("stt") or {"provider": ""}),
        embedding_config=cast(dict[str, object], payload.get("embedding") or {}),
        llm_realtime=cast(dict[str, object], llm_payload.get("realtime") or {"provider": "openrouter"}),
        llm_backend=cast(dict[str, object], llm_payload.get("backend") or {"provider": "openrouter"}),
        openrouter_key=providers.openrouter.apiKey if providers.openrouter else None,
        owner_phone=agent.owner.phone,
    )


async def _run_prepare_dependencies() -> None:
    global _prepare_task

    from mystic.cli import ensure_dependencies

    async def _on_prepare_step(label: str) -> None:
        await broadcast("prepare.step", {"label": label})

    def _on_prepare_detail(message: str, replace: bool) -> None:
        _broadcast_sync("prepare.detail", {"message": message, "replace": replace})

    await broadcast("prepare.started", {"status": "started"})
    try:
        await ensure_dependencies(
            _build_prepare_selections(),
            on_step=_on_prepare_step,
            on_detail=_on_prepare_detail,
            quiet=True,
        )
    except Exception as exc:
        error = get_error_message(exc)
        logger.warn("dashboard.prepare.error", error=error)
        await broadcast("prepare.error", {"error": error})
    else:
        await broadcast("prepare.done", {"status": "ready"})
        if _setup_done is not None:
            _setup_done.set()
    finally:
        _prepare_task = None


def create_setup_app(db: sqlite3.Connection) -> web.Application:
    app = web.Application()
    register_dashboard_routes(app, db)
    return app


def register_dashboard_routes(app: web.Application, db: sqlite3.Connection) -> None:
    seed_dashboard_defaults()
    ensure_dashboard_token()
    register_event_listener(_broadcast_sync)
    if cleanup_dashboard_voice_session not in app.on_cleanup:
        app.on_cleanup.append(cleanup_dashboard_voice_session)
    if _shutdown_dashboard_streams not in app.on_shutdown:
        app.on_shutdown.append(_shutdown_dashboard_streams)

    async def dashboard_root(_request: web.Request) -> web.Response:
        raise web.HTTPFound(DEFAULT_DASHBOARD_PATH)

    async def login_get(request: web.Request) -> web.Response:
        token = request.query.get("token", "").strip()
        next_path = request.query.get("next", DEFAULT_DASHBOARD_PATH).strip() or DEFAULT_DASHBOARD_PATH
        if token and token == ensure_dashboard_token():
            response = web.HTTPFound(next_path)
            response.set_cookie(
                SESSION_COOKIE,
                build_session_cookie(token),
                httponly=True,
                samesite="Strict",
                max_age=7 * 24 * 60 * 60,
                path="/",
            )
            raise response

        content = render_dashboard_template(
            "login.html",
            {"next_path": next_path},
        )
        return web.Response(
            text=render_shell(content, title="Login", current_path="/dashboard/login"),
            content_type="text/html",
        )

    async def login_post(request: web.Request) -> web.Response:
        form = await request.post()
        token = str(form.get("token", "")).strip()
        next_path = str(form.get("next", DEFAULT_DASHBOARD_PATH)).strip() or DEFAULT_DASHBOARD_PATH
        if token != ensure_dashboard_token():
            content = render_dashboard_template(
                "login.html",
                {"error": "Invalid dashboard token.", "next_path": next_path},
            )
            return web.Response(
                text=render_shell(content, title="Login", current_path="/dashboard/login"),
                content_type="text/html",
                status=401,
            )
        response = web.HTTPFound(next_path)
        response.set_cookie(
            SESSION_COOKIE,
            build_session_cookie(token),
            httponly=True,
            samesite="Strict",
            max_age=7 * 24 * 60 * 60,
            path="/",
        )
        raise response

    async def logout_post(_request: web.Request) -> web.Response:
        response = web.HTTPFound("/dashboard/login")
        response.del_cookie(SESSION_COOKIE, path="/")
        raise response

    async def setup_page(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        _maybe_import_sibling_twilio_credentials()
        agent = get_agent_config()
        providers = get_providers_config()

        openrouter_key = providers.openrouter.apiKey if providers.openrouter else ""
        deepgram_key = str(getattr(providers.stt, "apiKey", "") or "")
        inworld_key = str(getattr(providers.tts, "apiKey", "") or "")

        if not openrouter_key or not deepgram_key or not inworld_key:
            from mystic.cli import discover_siblings, extract_sibling_keys
            siblings = discover_siblings(get_home().name)
            if siblings:
                sibling_keys = extract_sibling_keys(siblings[0])
                if not openrouter_key:
                    openrouter_key = sibling_keys.get("openrouter", "")
                if not deepgram_key:
                    deepgram_key = sibling_keys.get("deepgram", "")
                if not inworld_key:
                    inworld_key = sibling_keys.get("inworld", "")

        llm_provider = str(getattr(providers.llm.realtime if providers.llm else None, "provider", "") or "")
        content = render_dashboard_template(
            "setup.html",
            {
                "openrouter_key": openrouter_key,
                "deepgram_key": deepgram_key,
                "inworld_key": inworld_key,
                "llm_mode": _setup_mode(llm_provider, local_providers={"custom", "local"}),
                "llm_model": _setup_llm_model(),
                "llm_local_url": _setup_llm_url(),
                "stt_mode": _setup_mode(getattr(providers.stt, "provider", ""), local_providers={"moonshine"}),
                "tts_mode": _setup_mode(getattr(providers.tts, "provider", ""), local_providers={"pocket"}),
                "tts_voice": agent.agent.voiceId or SETUP_DEFAULT_TTS_VOICE,
                "has_twilio": "true" if providers.twilio is not None else "false",
                "prepare_in_progress": "true" if _prepare_in_progress() else "false",
            },
        )
        return web.Response(
            text=render_setup_shell(content, title="Setup"),
            content_type="text/html",
        )

    async def setup_post(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        form = await request.post()
        agent = get_agent_config()
        providers = get_providers_config()
        llm_mode = str(form.get("llm_mode", "cloud")).strip().lower()
        stt_mode = str(form.get("stt_mode", "cloud")).strip().lower()
        tts_mode = str(form.get("tts_mode", "cloud")).strip().lower()
        llm_model = str(form.get("llm_model", "")).strip() or _setup_llm_model()
        llm_local_url = str(form.get("llm_local_url", "")).strip().rstrip("/") or SETUP_DEFAULT_LLM_URL
        openrouter_key = str(form.get("openrouter_key", providers.openrouter.apiKey if providers.openrouter else "")).strip()
        deepgram_key = str(form.get("deepgram_key", getattr(providers.stt, "apiKey", "") or "")).strip()
        inworld_key = str(form.get("inworld_key", getattr(providers.tts, "apiKey", "") or "")).strip()
        selected_voice = str(form.get("tts_voice", agent.agent.voiceId or SETUP_DEFAULT_TTS_VOICE)).strip()

        errors: dict[str, str] = {}
        if llm_mode == "cloud" and not openrouter_key:
            errors["openrouter_key"] = "OpenRouter API key is required for cloud LLM."
        if stt_mode == "cloud" and not deepgram_key:
            errors["deepgram_key"] = "Deepgram API key is required for cloud STT."
        if tts_mode == "cloud" and not inworld_key:
            errors["inworld_key"] = "Inworld API key is required for cloud TTS."
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=422)

        agent_payload = {
            **asdict(agent),
            "agent": {
                "name": agent.agent.name,
                "voiceId": selected_voice or SETUP_DEFAULT_TTS_VOICE,
            },
        }

        providers_payload = {
            **_serialize_config(providers),
        }
        if openrouter_key:
            providers_payload["openrouter"] = {"apiKey": openrouter_key}

        if llm_mode == "local":
            providers_payload["llm"] = {
                "realtime": {
                    "provider": "custom",
                    "baseURL": llm_local_url,
                    "model": llm_model,
                },
                "backend": {
                    "provider": "custom",
                    "baseURL": llm_local_url,
                    "model": llm_model,
                },
            }
        else:
            providers_payload["llm"] = {
                "realtime": {"provider": "openrouter", "model": llm_model},
                "backend": {"provider": "openrouter", "model": llm_model},
            }

        if stt_mode == "local":
            providers_payload["stt"] = {"provider": "moonshine", "model": "medium"}
        else:
            providers_payload["stt"] = {"provider": "deepgram", "apiKey": deepgram_key}

        if tts_mode == "local":
            providers_payload["tts"] = {"provider": "pocket"}
        else:
            providers_payload["tts"] = {"provider": "inworld", "apiKey": inworld_key}

        write_config("agent.json", agent_payload)
        write_config("providers.json", providers_payload)
        _update_setup_intelligence_model(llm_model)
        return web.json_response({"ok": True})

    async def settings_page(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        from mystic.http import check_tailscale_ready

        warnings = _dashboard_setup_warnings(request)
        _maybe_import_sibling_twilio_credentials()
        agent = get_agent_config()
        providers = get_providers_config()
        twilio = providers.twilio
        twilio_draft = providers.twilioDraft
        smtp = providers.smtp
        selected_voice_id = agent.agent.voiceId or SETUP_DEFAULT_TTS_VOICE
        readiness = _voice_readiness()
        stt_provider = cast(str, readiness["stt_provider"])
        tts_provider = cast(str, readiness["tts_provider"])
        stt_ready = bool(readiness["stt_ready"])
        tts_ready = bool(readiness["tts_ready"])
        embedding_ready = bool(readiness["embedding_ready"])
        voice_setup_needed = (
            (stt_provider == "moonshine" and not stt_ready)
            or (tts_provider == "pocket" and not tts_ready)
            or not embedding_ready
        )
        prepare_in_progress = _prepare_in_progress()
        ts_ready, ts_reason = check_tailscale_ready()
        content = render_dashboard_template(
            "settings.html",
            {
                "agent_name": agent.agent.name,
                "voice_id": selected_voice_id,
                "voice_is_custom": selected_voice_id not in SETTINGS_VOICE_OPTIONS,
                "voice_is_hades": selected_voice_id == "Hades",
                "voice_is_mark": selected_voice_id == "Mark",
                "voice_is_clive": selected_voice_id == "Clive",
                "voice_is_olivia": selected_voice_id == "Olivia",
                "voice_is_orietta": selected_voice_id == "Orietta",
                "voice_is_pippa": selected_voice_id == "Pippa",
                "owner_phone": agent.owner.phone or "",
                "openrouter_key": providers.openrouter.apiKey if providers.openrouter else "",
                "stt_provider": stt_provider,
                "tts_provider": tts_provider,
                "stt_ready": stt_ready,
                "tts_ready": tts_ready,
                "embedding_ready": embedding_ready,
                "stt_unconfigured": not stt_provider,
                "tts_unconfigured": not tts_provider,
                "stt_missing": bool(stt_provider) and not stt_ready,
                "tts_missing": bool(tts_provider) and not tts_ready,
                "embedding_missing": not embedding_ready,
                "voice_setup_needed": voice_setup_needed,
                "show_voice_setup": voice_setup_needed or prepare_in_progress,
                "stt_is_none": not stt_provider,
                "stt_is_moonshine": stt_provider == "moonshine",
                "stt_is_deepgram": stt_provider == "deepgram",
                "tts_is_none": not tts_provider,
                "tts_is_pocket": tts_provider == "pocket",
                "tts_is_inworld": tts_provider == "inworld",
                "show_deepgram_key": stt_provider == "deepgram",
                "show_inworld_key": tts_provider == "inworld",
                "deepgram_key": str(getattr(providers.stt, "apiKey", "") or ""),
                "inworld_key": str(getattr(providers.tts, "apiKey", "") or ""),
                "prepare_in_progress": prepare_in_progress,
                "twilio_sid": twilio.accountSid if twilio else (twilio_draft.accountSid if twilio_draft else ""),
                "twilio_phone": twilio.phoneNumber if twilio else "",
                "twilio_configured": twilio is not None,
                "twilio_draft_ready": twilio is None and twilio_draft is not None,
                "smtp_host": smtp.host if smtp else "",
                "smtp_port": smtp.port if smtp else "",
                "smtp_username": smtp.username if smtp else "",
                "smtp_from": smtp.from_address if smtp else "",
                "tailscale_ready": ts_ready,
                "tailscale_reason": ts_reason or "",
                "tailscale_url": get_tunnel_url() or "",
            },
        )
        content = _with_dashboard_warnings(content, warnings)
        return web.Response(
            text=render_shell(
                content,
                title="Settings",
                current_path="/dashboard/settings",
                hx_request=_is_hx_request(request),
            ),
            content_type="text/html",
        )

    async def settings_post(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        form = await request.post()
        _update_basic_settings(form)
        _update_voice_settings(form)
        _update_twilio_settings(form)
        _update_smtp_settings(form)
        raise web.HTTPFound("/dashboard/settings")

    async def prepare_dependencies(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        global _prepare_task

        if _prepare_in_progress():
            return web.Response(text=_prepare_fragment("Voice setup is already running."), content_type="text/html")

        _prepare_task = asyncio.create_task(_run_prepare_dependencies(), name="dashboard-prepare")
        return web.Response(text=_prepare_fragment("Setting up voice..."), content_type="text/html")

    async def dashboard_page(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        warnings = _dashboard_setup_warnings(request)
        slug = request.match_info["slug"].strip().lower()
        page_name = f"pages/{slug}.html"
        try:
            content = read_dashboard_file(page_name)
        except FileNotFoundError:
            raise web.HTTPNotFound(text=f"Dashboard page not found: {page_name}") from None
        content = _with_dashboard_warnings(content, warnings)
        return web.Response(
            text=render_shell(
                content,
                title=slug.replace("-", " ").title(),
                current_path=f"/dashboard/page/{slug}",
                hx_request=_is_hx_request(request),
            ),
            content_type="text/html",
        )

    async def dashboard_stream(request: web.Request) -> web.StreamResponse:
        require_dashboard_auth(request)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        _SSE_CLIENTS.add(queue)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        try:
            await response.prepare(request)
            if not await _write_sse(response, b": connected\n\n"):
                return response
            while True:
                payload = await queue.get()
                if payload is None:
                    return response
                if not await _write_sse(response, payload):
                    return response
        finally:
            _SSE_CLIENTS.discard(queue)
        return response

    async def voice_token(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        participant_name = f"dashboard-{secrets.token_hex(8)}"
        try:
            session = await acquire_dashboard_voice_session(
                app,
                db,
                participant_name=participant_name,
            )
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=get_error_message(exc)) from exc
        config = get_providers_config().livekit
        token = await generate_token(config, session.room_name, participant_name)
        request_host = request.host.split(":")[0]
        url = f"ws://{request_host}:{config.port}"

        chat_call_id, history = _load_dashboard_chat_history(db, session.person_id)
        hud_history = _load_call_history(db, session.call_id)

        await broadcast(
            "activity",
            {
                "type": "live_voice_connected",
                "call_id": session.call_id,
                "participant": participant_name,
                **interaction_event_payload(
                    describe_interaction(
                        direction="inbound",
                        channel="dashboard",
                        modality="voice",
                    )
                ),
            },
        )
        return web.json_response({
            "token": token,
            "url": url,
            "roomName": session.room_name,
            "callId": session.call_id,
            "chatCallId": chat_call_id,
            "participantName": participant_name,
            "history": history,
            "hudHistory": hud_history,
        })

    async def voice_history(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        session = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        if not isinstance(session, DashboardVoiceSession):
            return web.json_response({"history": []})
        chat_call_id, history = _load_dashboard_chat_history(db, session.person_id)
        hud_history = _load_call_history(db, session.call_id)
        return web.json_response({
            "history": history,
            "hudHistory": hud_history,
            "callId": session.call_id,
            "chatCallId": chat_call_id,
        })

    async def voice_disconnect(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            raw_participant_name = body.get("participantName")
            participant_name = (
                raw_participant_name.strip()
                if isinstance(raw_participant_name, str)
                else None
            )
        except Exception:
            call_id = ""
            participant_name = None

        session = app.get(_DASHBOARD_VOICE_SESSION_KEY)
        if session is not None and session.call_id == call_id:
            await broadcast(
                "activity",
                {"type": "live_voice_disconnected", "call_id": call_id},
            )
            await release_dashboard_voice_session(
                app,
                session,
                participant_name=participant_name,
            )
        return web.json_response({"ok": True})

    async def fragment_status(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        people = len(get_all_people(db, 500))
        pending = len(get_all_pending_actions(db))
        active = count_active_calls(db)
        payload = (
            '<section class="panel">'
            f"<h2>Agent</h2><p>{html.escape(get_agent_config().agent.name)}</p>"
            f"<p>People: {people}</p><p>Pending actions: {pending}</p><p>Active calls: {active}</p>"
            "</section>"
        )
        return web.Response(text=payload, content_type="text/html")

    async def fragment_active_calls(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        states = list_active_calls(db)
        if not states:
            return web.Response(text="<p>No active calls.</p>", content_type="text/html")
        items = "".join(
            f"<li>{html.escape(state.person_name or 'Unknown')} "
            f"({html.escape(format_interaction_brief(describe_call(state)))})</li>"
            for state in states
        )
        return web.Response(text=f"<ul>{items}</ul>", content_type="text/html")

    async def fragment_calls(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        limit = _optional_int(request.query.get("limit"), default=20, minimum=1, maximum=200)
        calls = get_recent_calls(db, limit)
        if not calls:
            return web.Response(text="<p>No calls yet.</p>", content_type="text/html")
        rows = "".join(
            _render_call_row(call)
            for call in calls
        )
        return web.Response(
            text=f"<table><thead><tr><th>Interaction</th><th>Direction</th><th>Audience</th><th>Started</th><th></th></tr></thead><tbody>{rows}</tbody></table>",
            content_type="text/html",
        )

    async def fragment_call(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        call = get_call_by_id(db, request.match_info["call_id"])
        if call is None:
            raise web.HTTPNotFound(text="Call not found")
        descriptor = describe_call(call)
        payload = (
            '<article class="panel">'
            f"<h2>Call {html.escape(call.id)}</h2>"
            f"<p>Channel: {html.escape(descriptor.channel_label)}</p>"
            f"<p>Modality: {html.escape(descriptor.modality_label)}</p>"
            f"<p>Direction: {html.escape(descriptor.direction_label)}</p>"
            f"<p>Audience: {html.escape(call.audience)}</p>"
            f"<pre>{html.escape(call.transcript or '')}</pre>"
            "</article>"
        )
        return web.Response(text=payload, content_type="text/html")

    async def fragment_people(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        query = request.query.get("q", "").strip()
        people = search_people(db, query) if query else get_all_people(db, 100)
        if not people:
            return web.Response(text="<p>No people found.</p>", content_type="text/html")
        rows = "".join(
            (
                "<tr>"
                f"<td>{html.escape(person.name or 'Unknown')}</td>"
                f"<td>{html.escape(person.phone)}</td>"
                f'<td><a href="/dashboard/f/person/{person.id}">Open</a></td>'
                "</tr>"
            )
            for person in people
        )
        return web.Response(
            text=f"<table><thead><tr><th>Name</th><th>Phone</th><th></th></tr></thead><tbody>{rows}</tbody></table>",
            content_type="text/html",
        )

    async def fragment_person(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        person = get_person_by_id(db, request.match_info["person_id"])
        if person is None:
            raise web.HTTPNotFound(text="Person not found")
        facts = get_all_active_facts_by_person(db, person.id, 20)
        calls = get_recent_calls_by_person(db, person.id, 10)
        facts_html = "".join(f"<li>{html.escape(fact.content)}</li>" for fact in facts) or "<li>No facts.</li>"
        calls_html = "".join(
            f"<li>{html.escape(call.id)} ({html.escape(describe_call(call).channel_label)})</li>"
            for call in calls
        ) or "<li>No calls.</li>"
        payload = (
            '<article class="panel">'
            f"<h2>{html.escape(person.name or 'Unknown')}</h2>"
            f"<p>{html.escape(person.phone)}</p>"
            "<h3>Facts</h3>"
            f"<ul>{facts_html}</ul>"
            "<h3>Calls</h3>"
            f"<ul>{calls_html}</ul>"
            "</article>"
        )
        return web.Response(text=payload, content_type="text/html")

    async def fragment_actions(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        status = request.query.get("status", "pending").strip() or "pending"
        if status not in {"pending", "in_progress", "completed", "failed", "cancelled"}:
            raise web.HTTPBadRequest(text="Unsupported action status")
        actions = (
            get_actions_by_status(db, cast(ActionStatus, status))
            if status != "pending"
            else get_all_pending_actions(db)
        )
        if not actions:
            return web.Response(text="<p>No actions found.</p>", content_type="text/html")
        rows = "".join(
            (
                "<tr>"
                f"<td>{html.escape(action.intent)}</td>"
                f"<td>{html.escape(action.status)}</td>"
                f'<td><a href="/dashboard/f/action/{action.id}">Open</a></td>'
                "</tr>"
            )
            for action in actions
        )
        return web.Response(
            text=f"<table><thead><tr><th>Intent</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table>",
            content_type="text/html",
        )

    async def fragment_action(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        action = get_action_by_id(db, request.match_info["action_id"])
        if action is None:
            raise web.HTTPNotFound(text="Action not found")
        payload = (
            '<article class="panel">'
            f"<h2>{html.escape(action.intent)}</h2>"
            f"<p>Status: {html.escape(action.status)}</p>"
            f"<p>Context: {html.escape(action.context or '')}</p>"
            "</article>"
        )
        return web.Response(text=payload, content_type="text/html")

    async def action_complete(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        action_id = request.match_info["action_id"]
        payload = await _load_request_payload(request)
        result = str(payload.get("result", "Completed from dashboard")).strip() or "Completed from dashboard"
        complete_action(db, action_id, result)
        await broadcast("activity", {"type": "action_completed", "action_id": action_id, "result": result})
        return web.json_response({"ok": True, "id": action_id, "status": "completed", "result": result})

    async def action_cancel(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        action_id = request.match_info["action_id"]
        payload = await _load_request_payload(request)
        reason = str(payload.get("reason", "Cancelled from dashboard")).strip() or "Cancelled from dashboard"
        cancel_action(db, action_id, reason)
        await broadcast("activity", {"type": "action_cancelled", "action_id": action_id, "reason": reason})
        return web.json_response({"ok": True, "id": action_id, "status": "cancelled", "reason": reason})

    async def graph_data(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        nodes: list[dict[str, object]] = []
        links: list[dict[str, object]] = []

        visible_limit = _optional_int(request.query.get("n"), default=24, minimum=1, maximum=200)
        window_days = _optional_int(request.query.get("window"), default=30, minimum=1, maximum=3650)
        now = int(time.time() * 1000)
        window_start = now - (window_days * 24 * 60 * 60 * 1000)
        day_ms = 24 * 60 * 60 * 1000

        agent = get_agent_config().agent
        identity_ready = identity_exists()
        soul_ready = soul_exists()
        journal_depth = len(list_journal_entries("identity", limit=100)) + len(
            list_journal_entries("soul", limit=100),
        )
        nodes.append({
            "id": "agent",
            "type": "agent",
            "label": agent.name,
            "weight": 1,
            "entityId": "agent",
            "identityReady": identity_ready,
            "soulReady": soul_ready,
            "journalDepth": journal_depth,
        })

        # Relationship graph: agent + people only. Facts, actions, and call
        # transcripts are content surfaced by the sidebar, not graph citizens.
        people_rows = db.execute(
            """
            WITH call_activity AS (
              SELECT
                person_id,
                MIN(started_at) AS first_interaction,
                MAX(started_at) AS last_interaction,
                COUNT(*) AS total_interactions,
                SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS recent_interactions
              FROM calls
              GROUP BY person_id
            ),
            fact_counts AS (
              SELECT person_id, COUNT(*) AS fact_count
              FROM facts
              WHERE superseded_at IS NULL
              GROUP BY person_id
            ),
            pending_actions AS (
              SELECT person_id, COUNT(*) AS pending_action_count
              FROM actions
              WHERE person_id IS NOT NULL
                AND status IN ('pending', 'in_progress')
              GROUP BY person_id
            )
            SELECT
              p.id,
              p.name,
              p.phone,
              p.summary,
              p.first_seen,
              p.last_seen,
              COALESCE(ca.first_interaction, p.first_seen) AS first_interaction,
              COALESCE(ca.last_interaction, p.last_seen) AS last_interaction,
              COALESCE(ca.total_interactions, 0) AS total_interactions,
              COALESCE(ca.recent_interactions, 0) AS recent_interactions,
              COALESCE(fc.fact_count, 0) AS fact_count,
              COALESCE(pa.pending_action_count, 0) AS pending_action_count
            FROM people p
            LEFT JOIN call_activity ca ON ca.person_id = p.id
            LEFT JOIN fact_counts fc ON fc.person_id = p.id
            LEFT JOIN pending_actions pa ON pa.person_id = p.id
            WHERE COALESCE(ca.total_interactions, 0) > 0
            ORDER BY first_interaction ASC, p.id ASC
            """,
            (window_start,),
        ).fetchall()

        def is_spam_only(person_id: str) -> bool:
            rows = db.execute(
                """
                SELECT summary, transcript
                FROM calls
                WHERE person_id = ?
                """,
                (person_id,),
            ).fetchall()
            if not rows:
                return False
            for call_row in rows:
                haystack = f"{call_row['summary'] or ''}\n{call_row['transcript'] or ''}".upper()
                if "SPAM" not in haystack:
                    return False
            return True

        people: list[dict[str, Any]] = []
        for row in people_rows:
            person_id = str(row["id"])
            if is_spam_only(person_id):
                continue
            angle_index = len(people)
            total_interactions = int(row["total_interactions"] or 0)
            recent_interactions = int(row["recent_interactions"] or 0)
            last_interaction = int(row["last_interaction"] or row["last_seen"] or 0)
            first_interaction = int(row["first_interaction"] or row["first_seen"] or 0)
            people.append({
                "id": person_id,
                "nodeId": f"p:{person_id}",
                "name": row["name"],
                "phone": row["phone"],
                "summary": row["summary"],
                "firstSeen": int(row["first_seen"] or 0),
                "lastSeen": int(row["last_seen"] or 0),
                "firstInteraction": first_interaction,
                "lastInteraction": last_interaction,
                "totalInteractions": total_interactions,
                "recentInteractions": recent_interactions,
                "factCount": int(row["fact_count"] or 0),
                "pendingActionCount": int(row["pending_action_count"] or 0),
                "angleIndex": angle_index,
            })

        visible_people = sorted(
            people,
            key=lambda item: (
                int(item["recentInteractions"]),
                int(item["lastInteraction"]),
                int(item["totalInteractions"]),
            ),
            reverse=True,
        )[:visible_limit]
        visible_by_id = {str(item["id"]): item for item in visible_people}

        # Return nodes in chronological slot order so the client can preserve
        # visible gaps when inactive people fall out of the recent-activity set.
        for item in sorted(visible_people, key=lambda p: int(p["angleIndex"])):
            identified = bool(str(item["name"] or "").strip())
            label = str(item["name"] or item["phone"] or "Unknown")
            angle = int(item["angleIndex"]) * 2.399963229728653
            nodes.append({
                "id": item["nodeId"],
                "type": "person",
                "label": label,
                "weight": max(1, int(item["totalInteractions"])),
                "entityId": item["id"],
                "phone": item["phone"],
                "summary": item["summary"],
                "identified": identified,
                "factCount": item["factCount"],
                "pendingActionCount": item["pendingActionCount"],
                "totalInteractions": item["totalInteractions"],
                "recentInteractions": item["recentInteractions"],
                "firstSeen": item["firstSeen"],
                "lastSeen": item["lastSeen"],
                "firstInteraction": item["firstInteraction"],
                "lastInteraction": item["lastInteraction"],
                "angleIndex": item["angleIndex"],
                "angle": angle,
                "radius": 180,
            })

        channel_rows = db.execute(
            """
            SELECT
              person_id,
              channel,
              modality,
              COUNT(*) AS total_count,
              SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS recent_count,
              MIN(started_at) AS first_started,
              MAX(started_at) AS last_started,
              SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound_count,
              SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound_count
            FROM calls
            GROUP BY person_id, channel, modality
            """,
            (window_start,),
        ).fetchall()

        for row in channel_rows:
            person_id = str(row["person_id"])
            person = visible_by_id.get(person_id)
            if person is None:
                continue
            channel = str(row["channel"])
            modality = str(row["modality"])
            descriptor = describe_interaction(direction="inbound", channel=channel, modality=modality)
            total_count = int(row["total_count"] or 0)
            recent_count = int(row["recent_count"] or 0)
            last_started = int(row["last_started"] or person["lastInteraction"] or 0)
            days_since = max(0.0, (now - last_started) / day_ms) if last_started else float(window_days)
            recency = max(0.0, min(1.0, 1.0 - (days_since / max(1, window_days))))
            links.append({
                "id": f"edge:{person_id}:{channel}:{modality}",
                "type": "strand",
                "source": "agent",
                "target": f"p:{person_id}",
                "personId": person_id,
                "channel": descriptor.channel,
                "channelLabel": descriptor.channel_label,
                "modality": descriptor.modality,
                "modalityLabel": descriptor.modality_label,
                "strandLabel": descriptor.label,
                "interactionLabel": descriptor.label,
                "total": total_count,
                "recentCount": recent_count,
                "inboundCount": int(row["inbound_count"] or 0),
                "outboundCount": int(row["outbound_count"] or 0),
                "firstInteraction": int(row["first_started"] or 0),
                "lastInteraction": last_started,
                "recency": recency,
                "weight": max(1.0, total_count ** 0.5) * (0.35 + recency * 0.65),
            })

        return web.json_response({
            "nodes": nodes,
            "links": links,
            "meta": {
                "visibleLimit": visible_limit,
                "activityWindowDays": window_days,
                "hiddenPeople": max(0, len(people) - len(visible_people)),
                "totalPeople": len(people),
            },
        })

    async def graph_node_detail(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        ntype = request.match_info["ntype"]
        entity_id = request.match_info["entity_id"]

        if ntype == "agent":
            identity = read_identity() if identity_exists() else None
            person_count = db.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"]
            call_count = db.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
            journal_depth = len(list_journal_entries("identity", limit=100)) + len(
                list_journal_entries("soul", limit=100),
            )
            agent = get_agent_config().agent
            return web.json_response({
                "type": "agent", "id": entity_id,
                "name": agent.name,
                "creature": identity.creature if identity else "",
                "vibe": identity.vibe if identity else "",
                "identityReady": identity is not None,
                "soulReady": soul_exists(),
                "journalDepth": journal_depth,
                "personCount": person_count,
                "callCount": call_count,
            })

        if ntype == "person":
            row = db.execute(
                "SELECT id, name, phone, summary, first_seen, last_seen FROM people WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if not row:
                raise web.HTTPNotFound()
            call_count = db.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE person_id = ?", (entity_id,),
            ).fetchone()["n"]
            fact_count = db.execute(
                """
                SELECT COUNT(*) AS n
                FROM facts
                WHERE person_id = ?
                  AND superseded_at IS NULL
                """,
                (entity_id,),
            ).fetchone()["n"]
            pending_action_count = db.execute(
                """
                SELECT COUNT(*) AS n
                FROM actions
                WHERE person_id = ?
                  AND status IN ('pending', 'in_progress')
                """,
                (entity_id,),
            ).fetchone()["n"]
            channel_rows = db.execute(
                """
                SELECT
                  channel,
                  modality,
                  COUNT(*) AS total_count,
                  MAX(started_at) AS last_started
                FROM calls
                WHERE person_id = ?
                GROUP BY channel, modality
                ORDER BY last_started DESC
                """,
                (entity_id,),
            ).fetchall()
            strands = []
            for channel_row in channel_rows:
                descriptor = describe_interaction(
                    direction="inbound",
                    channel=channel_row["channel"],
                    modality=channel_row["modality"],
                )
                strands.append({
                    "channel": descriptor.channel,
                    "channelLabel": descriptor.channel_label,
                    "modality": descriptor.modality,
                    "modalityLabel": descriptor.modality_label,
                    "strandLabel": descriptor.label,
                    "total": channel_row["total_count"],
                    "lastInteraction": channel_row["last_started"],
                })
            return web.json_response({
                "type": "person", "id": row["id"],
                "name": row["name"], "phone": row["phone"],
                "identified": bool(str(row["name"] or "").strip()),
                "summary": row["summary"], "callCount": call_count,
                "factCount": fact_count,
                "pendingActionCount": pending_action_count,
                "strands": strands,
                "firstSeen": row["first_seen"], "lastSeen": row["last_seen"],
            })

        if ntype == "call":
            row = db.execute(
                "SELECT id, direction, channel, modality, audience, summary, started_at, answered_at FROM calls WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if not row:
                raise web.HTTPNotFound()
            descriptor = describe_interaction(
                direction=row["direction"],
                channel=row["channel"],
                modality=row["modality"],
            )
            return web.json_response({
                "type": "call", "id": row["id"],
                "direction": row["direction"], "audience": row["audience"],
                "directionLabel": descriptor.direction_label,
                "channel": descriptor.channel,
                "channelLabel": descriptor.channel_label,
                "modality": descriptor.modality,
                "modalityLabel": descriptor.modality_label,
                "interactionLabel": descriptor.label,
                "summary": row["summary"],
                "startedAt": row["started_at"], "answeredAt": row["answered_at"],
            })

        if ntype == "fact":
            row = db.execute(
                "SELECT id, type, content, confidence, source, created_at FROM facts WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if not row:
                raise web.HTTPNotFound()
            return web.json_response({
                "type": "fact", "id": row["id"],
                "factType": row["type"], "content": row["content"],
                "confidence": row["confidence"], "source": row["source"],
                "createdAt": row["created_at"],
            })

        if ntype == "action":
            row = db.execute(
                "SELECT id, intent, context, status, urgency, due_at, source_text,"
                " attempts, max_attempts, created_at FROM actions WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if not row:
                raise web.HTTPNotFound()
            return web.json_response({
                "type": "action", "id": row["id"],
                "intent": row["intent"], "context": row["context"],
                "status": row["status"], "urgency": row["urgency"],
                "dueAt": row["due_at"], "sourceText": row["source_text"],
                "attempts": row["attempts"], "maxAttempts": row["max_attempts"],
                "createdAt": row["created_at"],
            })

        raise web.HTTPNotFound()

    async def graph_thread(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        person_id = request.match_info["person_id"]
        channel = request.match_info["channel"]
        modality = request.match_info.get("modality")
        limit = _optional_int(request.query.get("limit"), default=20, minimum=1, maximum=100)
        if channel not in {"dashboard", "phone", "sms", "cli"}:
            raise web.HTTPBadRequest(text="Unsupported graph channel")
        if modality is not None and modality not in {"voice", "text", "mixed"}:
            raise web.HTTPBadRequest(text="Unsupported graph modality")

        person = get_person_by_id(db, person_id)
        if person is None:
            raise web.HTTPNotFound(text="Person not found")

        if modality is None:
            channel_clause = "channel = ?"
            params: tuple[object, ...] = (person_id, channel, limit)
        else:
            channel_clause = "channel = ? AND modality = ?"
            params = (person_id, channel, modality, limit)

        rows = db.execute(
            f"""
            SELECT id, direction, channel, modality, audience, summary,
                   transcript, started_at, answered_at, ended_at
            FROM calls
            WHERE person_id = ?
              AND {channel_clause}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        calls: list[dict[str, object]] = []
        for row in rows:
            descriptor = describe_interaction(
                direction=row["direction"],
                channel=row["channel"],
                modality=row["modality"],
            )
            transcript = str(row["transcript"] or "")
            transcript_preview = "\n".join(transcript.splitlines()[:8])
            calls.append({
                "id": row["id"],
                "summary": row["summary"],
                "transcriptPreview": transcript_preview,
                "direction": row["direction"],
                "directionLabel": descriptor.direction_label,
                "channel": descriptor.channel,
                "channelLabel": descriptor.channel_label,
                "modality": descriptor.modality,
                "modalityLabel": descriptor.modality_label,
                "interactionLabel": descriptor.label,
                "audience": row["audience"],
                "startedAt": row["started_at"],
                "answeredAt": row["answered_at"],
                "endedAt": row["ended_at"],
            })

        return web.json_response({
            "type": "thread",
            "personId": person.id,
            "personName": person.name,
            "personPhone": person.phone,
            "channel": channel,
            "channelLabel": _graph_channel_label(channel),
            "modality": modality,
            "modalityLabel": modality.title() if modality else None,
            "strandLabel": _graph_strand_label(channel, modality) if modality else _graph_channel_label(channel),
            "calls": calls,
        })

    async def graph_person_rename(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        person_id = request.match_info["person_id"]
        payload = await _load_request_payload(request)
        name = str(payload.get("name", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text="Name is required")
        if len(name) > 120:
            raise web.HTTPBadRequest(text="Name is too long")
        row = db.execute("SELECT id FROM people WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(text="Person not found")
        with db:
            db.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
        await broadcast("activity", {"type": "person_renamed", "person_id": person_id, "name": name})
        return web.json_response({"ok": True, "id": person_id, "name": name})

    async def game_scores_get(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        scores = top_game_scores(db, limit=10)
        return web.json_response({
            "scores": [
                {
                    "name": s.name,
                    "score": s.score,
                    "wave": s.wave,
                    "created_at": s.created_at,
                }
                for s in scores
            ],
        })

    async def game_scores_post(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        payload = await _load_request_payload(request)
        raw_name = str(payload.get("name", "")).strip().upper()
        name = "".join(c for c in raw_name if "A" <= c <= "Z")[:3]
        if not name:
            raise web.HTTPBadRequest(text="Initials required (A-Z)")
        try:
            score = int(str(payload.get("score", 0)))
            wave = int(str(payload.get("wave", 1)))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Score and wave must be integers")
        if score < 0 or score > 10_000_000:
            raise web.HTTPBadRequest(text="Score out of range")
        if wave < 1 or wave > 1000:
            raise web.HTTPBadRequest(text="Wave out of range")
        prev_best = previous_best_game_score(db)
        saved = insert_game_score(db, name=name, score=score, wave=wave)
        rank = rank_for_score(db, score)
        return web.json_response({
            "ok": True,
            "id": saved.id,
            "name": saved.name,
            "score": saved.score,
            "wave": saved.wave,
            "rank": rank,
            "prev_best": prev_best,
        })

    async def game_token(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        agent = get_agent_config()
        config = get_providers_config().livekit
        slug = _slugify_dashboard_room_part(agent.agent.name)
        room_name = f"game-{slug}-{secrets.token_hex(4)}"
        participant_name = f"pilot-{secrets.token_hex(6)}"
        metadata: dict[str, object] = {
            "kind": "game",
            "audience": "public",
            "direction": "inbound",
            "channel": "dashboard",
            "modality": "voice",
        }
        try:
            await _create_dashboard_room_with_agent_dispatch(config, room_name, metadata)
        except Exception as exc:
            raise web.HTTPServiceUnavailable(text=get_error_message(exc)) from exc
        token = await generate_token(config, room_name, participant_name)
        request_host = request.host.split(":")[0]
        url = f"ws://{request_host}:{config.port}"
        return web.json_response({
            "token": token,
            "url": url,
            "roomName": room_name,
            "participantName": participant_name,
        })

    async def static_asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name == "style.css":
            path = get_dashboard_style_path()
            if not path.exists():
                path = get_dashboard_defaults_dir() / "style.css"
        elif name == "manifest.json":
            path = get_dashboard_manifest_path()
            if not path.exists():
                path = get_dashboard_defaults_dir() / "manifest.json"
        else:
            path = get_static_dir() / name
        if not path.exists() or not path.is_file():
            raise web.HTTPNotFound(text=f"Static asset not found: {name}")
        content_type, _ = mimetypes.guess_type(path.name)
        return web.Response(
            body=path.read_bytes(),
            content_type=content_type or "application/octet-stream",
        )

    async def soundfx_asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        allowed = {"highendSwitchOn.ogg", "highendSwitchOff.ogg"}
        if name not in allowed:
            raise web.HTTPNotFound(text=f"Sound effect not found: {name}")
        path = get_soundfx_dir() / name
        if not path.exists() or not path.is_file():
            raise web.HTTPNotFound(text=f"Sound effect not found: {name}")
        content_type, _ = mimetypes.guess_type(path.name)
        return web.Response(
            body=path.read_bytes(),
            content_type=content_type or "application/octet-stream",
        )

    async def calendar_list(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        events = get_recent_external_events(db, _optional_int(request.query.get("limit"), default=50, minimum=1, maximum=200))
        items = "".join(
            f"<li>{html.escape(event.title)} ({event.start_at} - {event.end_at})</li>"
            for event in events
        ) or "<li>No calendar items.</li>"
        return web.Response(text=f"<ul>{items}</ul>", content_type="text/html")

    async def calendar_get(request: web.Request) -> web.Response:
        require_dashboard_auth(request)
        event = get_external_event_by_id(db, request.match_info["event_id"])
        if event is None:
            raise web.HTTPNotFound(text="Calendar item not found")
        payload = (
            '<article class="panel">'
            f"<h2>{html.escape(event.title)}</h2>"
            f"<p>{event.start_at} - {event.end_at}</p>"
            f"<p>{html.escape(event.description or '')}</p>"
            "</article>"
        )
        return web.Response(text=payload, content_type="text/html")

    app.router.add_get("/dashboard", dashboard_root)
    app.router.add_get("/dashboard/login", login_get)
    app.router.add_post("/dashboard/login", login_post)
    app.router.add_post("/dashboard/logout", logout_post)
    app.router.add_get("/dashboard/setup", setup_page)
    app.router.add_post("/dashboard/setup", setup_post)
    app.router.add_get("/dashboard/settings", settings_page)
    app.router.add_post("/dashboard/settings", settings_post)
    app.router.add_post("/dashboard/f/prepare", prepare_dependencies)
    app.router.add_get("/dashboard/page/{slug}", dashboard_page)
    app.router.add_get("/dashboard/stream", dashboard_stream)
    app.router.add_post("/dashboard/api/voice/token", voice_token)
    app.router.add_post("/dashboard/api/voice/disconnect", voice_disconnect)
    app.router.add_get("/dashboard/api/voice/history", voice_history)
    app.router.add_get("/dashboard/f/status", fragment_status)
    app.router.add_get("/dashboard/f/active-calls", fragment_active_calls)
    app.router.add_get("/dashboard/f/calls", fragment_calls)
    app.router.add_get("/dashboard/f/call/{call_id}", fragment_call)
    app.router.add_get("/dashboard/f/people", fragment_people)
    app.router.add_get("/dashboard/f/person/{person_id}", fragment_person)
    app.router.add_get("/dashboard/f/actions", fragment_actions)
    app.router.add_get("/dashboard/f/action/{action_id}", fragment_action)
    app.router.add_post("/dashboard/f/action/{action_id}/complete", action_complete)
    app.router.add_post("/dashboard/f/action/{action_id}/cancel", action_cancel)
    app.router.add_get("/dashboard/api/graph", graph_data)
    app.router.add_get("/dashboard/api/graph/node/{ntype}/{entity_id}", graph_node_detail)
    app.router.add_get("/dashboard/api/graph/thread/{person_id}/{channel}/{modality}", graph_thread)
    app.router.add_get("/dashboard/api/graph/thread/{person_id}/{channel}", graph_thread)
    app.router.add_post("/dashboard/api/graph/person/{person_id}/name", graph_person_rename)
    app.router.add_get("/dashboard/api/game/scores", game_scores_get)
    app.router.add_post("/dashboard/api/game/scores", game_scores_post)
    app.router.add_post("/dashboard/api/game/token", game_token)
    app.router.add_get("/dashboard/f/calendar", calendar_list)
    app.router.add_get("/dashboard/f/calendar/{event_id}", calendar_get)
    app.router.add_get("/static/{name}", static_asset)
    app.router.add_get("/soundfx/{name}", soundfx_asset)


def _update_basic_settings(form: Mapping[str, Any]) -> None:
    agent = get_agent_config()
    providers = get_providers_config()

    agent_payload = {
        **asdict(agent),
        "agent": {
            "name": str(form.get("agent_name", agent.agent.name)).strip() or agent.agent.name,
            "voiceId": str(form.get("voice_id", agent.agent.voiceId or "")).strip() or None,
        },
    }
    owner_phone = str(form.get("owner_phone", agent.owner.phone or "")).strip()
    agent_payload["owner"] = {"phone": owner_phone or None}
    write_config("agent.json", agent_payload)

    providers_payload = {
        **_serialize_config(providers),
    }
    openrouter_key = str(form.get("openrouter_key", providers.openrouter.apiKey if providers.openrouter else "")).strip()
    if openrouter_key:
        providers_payload["openrouter"] = {"apiKey": openrouter_key}
    write_config("providers.json", providers_payload)


def _update_voice_settings(form: Mapping[str, Any]) -> None:
    providers = get_providers_config()
    payload = {
        **_serialize_config(providers),
    }

    stt_provider = str(form.get("stt_provider", getattr(providers.stt, "provider", ""))).strip().lower()
    if stt_provider == "moonshine":
        existing_model = str(getattr(providers.stt, "model", "") or "").strip().lower() or "small"
        payload["stt"] = {"provider": "moonshine", "model": existing_model}
    elif stt_provider == "deepgram":
        payload["stt"] = {"provider": "deepgram", "apiKey": str(form.get("deepgram_key", "")).strip()}
    else:
        payload["stt"] = {"provider": ""}

    tts_provider = str(form.get("tts_provider", getattr(providers.tts, "provider", ""))).strip().lower()
    if tts_provider == "pocket":
        payload["tts"] = {"provider": "pocket"}
    elif tts_provider == "inworld":
        payload["tts"] = {"provider": "inworld", "apiKey": str(form.get("inworld_key", "")).strip()}
    else:
        payload["tts"] = {"provider": ""}

    write_config("providers.json", payload)


def _update_twilio_settings(form: Mapping[str, Any]) -> None:
    providers = get_providers_config()
    payload = {
        **_serialize_config(providers),
    }
    existing = providers.twilio
    draft = providers.twilioDraft
    account_sid_default = existing.accountSid if existing else (draft.accountSid if draft else "")
    auth_token_default = existing.authToken if existing else (draft.authToken if draft else "")
    phone_number_default = existing.phoneNumber if existing else ""
    account_sid = str(form.get("twilio_sid", account_sid_default)).strip()
    auth_token_form = str(form.get("twilio_auth_token", "")).strip()
    auth_token = auth_token_form or (auth_token_default if account_sid == account_sid_default else "")
    phone_number = str(form.get("twilio_phone", phone_number_default)).strip()
    if account_sid and auth_token and phone_number:
        twilio_payload: dict[str, object] = {
            "accountSid": account_sid,
            "authToken": auth_token,
            "phoneNumber": phone_number,
        }
        if (
            existing
            and existing.phoneNumberSid
            and existing.accountSid == account_sid
            and existing.phoneNumber == phone_number
        ):
            twilio_payload["phoneNumberSid"] = existing.phoneNumberSid
        payload["twilio"] = twilio_payload
        payload.pop("twilioDraft", None)
    elif account_sid and auth_token:
        payload.pop("twilio", None)
        payload["twilioDraft"] = {
            "accountSid": account_sid,
            "authToken": auth_token,
        }
    else:
        payload.pop("twilio", None)
        payload.pop("twilioDraft", None)
    write_config("providers.json", payload)


def _update_smtp_settings(form: Mapping[str, Any]) -> None:
    providers = get_providers_config()
    payload = {
        **_serialize_config(providers),
    }
    existing = providers.smtp
    host = str(form.get("smtp_host", existing.host if existing else "")).strip()
    port = str(form.get("smtp_port", existing.port if existing else "")).strip()
    username = str(form.get("smtp_username", existing.username if existing else "")).strip()
    password = str(form.get("smtp_password", "")).strip() or (existing.password if existing else "")
    from_address = str(form.get("smtp_from", existing.from_address if existing else "")).strip()
    if host and port and username and password and from_address:
        payload["smtp"] = {
            "host": host,
            "port": int(port),
            "username": username,
            "password": password,
            "fromAddress": from_address,
            "useTls": True,
        }
    else:
        payload["smtp"] = None
    write_config("providers.json", payload)


async def _load_request_payload(request: web.Request) -> dict[str, object]:
    if request.can_read_body:
        content_type = request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = await request.json()
            if isinstance(payload, dict):
                return dict(payload)
        form = await request.post()
        return {key: value for key, value in form.items()}
    return {}


def _optional_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _graph_channel_label(channel: str) -> str:
    return {
        "dashboard": "Dashboard",
        "phone": "Phone",
        "sms": "SMS",
        "cli": "CLI",
        "chat": "Chat",
    }.get(channel, channel.title())


def _graph_strand_label(channel: str, modality: str | None) -> str:
    if modality is None:
        return _graph_channel_label(channel)
    descriptor = describe_interaction(direction="inbound", channel=channel, modality=modality)
    return descriptor.label


def search_people(db: sqlite3.Connection, query: str):
    from mystic.db import find_people

    return find_people(db, query)
