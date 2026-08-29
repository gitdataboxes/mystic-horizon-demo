"""Calendar sync, availability, and reminders."""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import time as time_module
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, urlencode
from typing import Any
from zoneinfo import ZoneInfo

from mystic.config import (
    CalendarHubConfig,
    CalendarSubscription,
    OAuthTokens,
    get_agent_config,
    get_calendar_config,
    get_calendar_hub_config,
    get_error_message,
    get_hub_tokens,
    logger,
    save_hub_tokens,
)
from mystic.db import (
    clear_action_hub_event,
    delete_stale_external_events,
    get_actions_pending_hub_sync,
    get_external_events_in_range,
    get_in_progress_scheduled_actions,
    get_scheduled_actions_in_range,
    get_upcoming_external_events,
    get_upcoming_scheduled_actions,
    increment_hub_sync_attempts,
    mark_action_hub_failed,
    mark_action_hub_synced,
    now_ms,
    upsert_external_event,
)
from mystic.http import fetch_with_timeout
from mystic.types import Action, Audience

_last_sync_at = 0
_last_refresh_at = 0.0
_notified_ids: set[str] = set()
_MAX_NOTIFIED_CACHE = 500
_HUB_MAX_RETRIES = 3

_AUTH_URLS = {
    "google": "https://accounts.google.com/o/oauth2/v2/auth",
    "microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
}
_TOKEN_URLS = {
    "google": "https://oauth2.googleapis.com/token",
    "microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}
_SCOPES = {
    "google": ["https://www.googleapis.com/auth/calendar.events"],
    "microsoft": ["Calendars.ReadWrite", "offline_access"],
}

HubCallFn = Callable[[CalendarHubConfig, str, Action], Awaitable[str | bool]]


def format_event_time(timestamp_ms: int, tz: ZoneInfo) -> str:
    """Format epoch-ms as a local wall-clock time."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=tz)
    return dt.strftime("%I:%M %p")


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def run_oauth_flow(
    *,
    auth_url: str,
    token_url: str,
    client_id: str,
    client_secret: str | None,
    scopes: list[str],
    extra_params: dict[str, str] | None = None,
) -> OAuthTokens:
    import hashlib
    import secrets
    import shutil
    import subprocess

    from aiohttp import web

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("utf-8")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    received: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    app = web.Application()

    async def callback_handler(request: web.Request) -> web.Response:
        if request.query.get("state") != state:
            return web.Response(text="State mismatch.", status=400)

        code = request.query.get("code")
        if not code:
            error = request.query.get("error", "unknown")
            if not received.done():
                received.set_exception(RuntimeError(f"OAuth error: {error}"))
            return web.Response(text=f"Authorization failed: {error}")

        if not received.done():
            received.set_result(code)
        return web.Response(text="Authorized. You can close this tab.")

    app.router.add_get("/callback", callback_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **(extra_params or {}),
    }
    auth_request_url = f"{auth_url}?{urlencode(params)}"
    logger.info("calendar.hub.oauth.start", authUrl=auth_request_url)
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        subprocess.Popen(
            [opener, auth_request_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        import webbrowser
        webbrowser.open(auth_request_url)

    try:
        code = await asyncio.wait_for(received, timeout=120)
    finally:
        await runner.cleanup()

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    response = await fetch_with_timeout(
        token_url,
        method="POST",
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout_ms=15_000,
        timeout_label="oauth-token-exchange",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"OAuth token exchange failed: HTTP {response.status_code}")
    data = response.json()
    return OAuthTokens(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token", "")),
        expires_at=int(time_module.time()) + int(data.get("expires_in", 3600)),
        token_type=str(data.get("token_type", "Bearer")),
    )


async def _refresh_oauth_token(hub: CalendarHubConfig, tokens: OAuthTokens) -> OAuthTokens:
    token_url = _TOKEN_URLS.get(hub.provider)
    if token_url is None:
        raise RuntimeError(f"Unsupported OAuth provider: {hub.provider}")
    if not tokens.refresh_token:
        raise RuntimeError("Hub calendar refresh token is missing. Reconnect the hub calendar.")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": hub.client_id or "",
    }
    if hub.client_secret:
        payload["client_secret"] = hub.client_secret

    response = await fetch_with_timeout(
        token_url,
        method="POST",
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout_ms=15_000,
        timeout_label="oauth-refresh",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Token refresh failed: HTTP {response.status_code}")
    data = response.json()
    return OAuthTokens(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token", tokens.refresh_token)),
        expires_at=int(time_module.time()) + int(data.get("expires_in", 3600)),
        token_type=str(data.get("token_type", "Bearer")),
    )


async def ensure_valid_token(hub: CalendarHubConfig, *, force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing it when needed."""
    global _last_refresh_at

    tokens = get_hub_tokens()
    if tokens is None:
        raise RuntimeError(
            "Hub calendar not authenticated. Run init --connect-hub-calendar."
        )

    if not force_refresh and tokens.expires_at > int(time_module.time()) + 60:
        return tokens.access_token

    if not force_refresh and time_module.monotonic() - _last_refresh_at < 5:
        refreshed = get_hub_tokens()
        if refreshed is not None and refreshed.expires_at > int(time_module.time()) + 60:
            return refreshed.access_token

    _last_refresh_at = time_module.monotonic()
    new_tokens = await _refresh_oauth_token(hub, tokens)
    save_hub_tokens(new_tokens)
    return new_tokens.access_token


def format_current_schedule(
    db: sqlite3.Connection,
    audience: Audience,
    tz: ZoneInfo,
    person_id: str | None = None,
    reference_ms: int | None = None,
) -> str:
    now = now_ms() if reference_ms is None else reference_ms
    current_items: list[str] = []

    if audience == "owner":
        for event in get_external_events_in_range(
            db,
            now - (60 * 60_000),
            now + (60 * 60_000),
            limit=10,
        ):
            current_items.append(f"{event.title} until {format_event_time(event.end_at, tz)}")

    for action in get_in_progress_scheduled_actions(db, limit=10):
        if audience != "owner" and action.person_id != person_id:
            continue
        if action.end_at is None:
            continue
        current_items.append(f"{action.intent} until {format_event_time(action.end_at, tz)}")

    if not current_items:
        return ""
    return "Currently: " + "; ".join(current_items)


def format_upcoming_schedule(
    db: sqlite3.Connection,
    audience: Audience,
    person_id: str | None,
    tz: ZoneInfo,
    window_ms: int,
    reference_ms: int | None = None,
) -> str:
    now = now_ms() if reference_ms is None else reference_ms
    upcoming_lines: list[str] = []

    if audience == "owner":
        for event in get_external_events_in_range(db, now, now + window_ms, limit=25):
            if event.start_at < now:
                continue
            upcoming_lines.append(f"- {_format_schedule_start(event.start_at, tz)}: {event.title}")

    for action in get_scheduled_actions_in_range(db, now, now + window_ms, limit=25):
        if action.start_at is None:
            continue
        if action.start_at < now:
            continue
        if audience != "owner" and action.person_id != person_id:
            continue
        upcoming_lines.append(f"- {_format_schedule_start(action.start_at, tz)}: {action.intent}")

    return "\n".join(upcoming_lines)


def parse_ics_feed(raw_ics: str) -> Any:
    try:
        from icalendar import Calendar  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on local env.
        raise RuntimeError(
            "Calendar support requires the 'icalendar' package. "
            "Install project dependencies to enable ICS sync."
        ) from exc
    return Calendar.from_ical(raw_ics)


def expand_events(cal: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    try:
        from recurring_ical_events import of as recurring_of  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on local env.
        raise RuntimeError(
            "Calendar support requires the 'recurring-ical-events' package. "
            "Install project dependencies to enable ICS sync."
        ) from exc

    events = recurring_of(cal).between(start, end)
    result: list[dict[str, Any]] = []
    for event in events:
        dtstart = event.get("DTSTART")
        dtend = event.get("DTEND")
        if dtstart is None:
            continue

        start_value = dtstart.dt
        all_day = not isinstance(start_value, datetime)
        if all_day:
            start_date = start_value if isinstance(start_value, date) else start_value.date()
            start_ms = _date_to_ms(start_date)

            if dtend is None:
                end_date = start_date + timedelta(days=1)
            else:
                end_value = dtend.dt
                end_date = end_value if isinstance(end_value, date) else end_value.date()
            end_ms = _date_to_ms(end_date)
        else:
            start_dt = _ensure_utc_datetime(start_value)
            start_ms = int(start_dt.timestamp() * 1000)
            if dtend is None:
                end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = _ensure_utc_datetime(dtend.dt)
            end_ms = int(end_dt.timestamp() * 1000)

        result.append(
            {
                "uid": str(event.get("UID", "")),
                "title": str(event.get("SUMMARY", "Untitled")),
                "description": str(event.get("DESCRIPTION", "")) or None,
                "location": str(event.get("LOCATION", "")) or None,
                "start_at": start_ms,
                "end_at": end_ms,
                "all_day": all_day,
            }
        )
    return result


async def sync_subscription(
    db: sqlite3.Connection,
    sub: CalendarSubscription,
    *,
    window_days: int = 90,
) -> int:
    response = await fetch_with_timeout(
        sub.url,
        timeout_ms=30_000,
        timeout_label="ics-sync",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"ICS sync failed with status {response.status_code}")

    cal = parse_ics_feed(response.text)
    now = datetime.now(UTC)
    events = expand_events(cal, now - timedelta(days=7), now + timedelta(days=window_days))

    seen_uids: set[str] = set()
    for event in events:
        uid = str(event["uid"])
        if uid:
            seen_uids.add(uid)
        upsert_external_event(
            db,
            ics_uid=uid,
            ics_url=sub.url,
            title=str(event["title"]),
            start_at=int(event["start_at"]),
            end_at=int(event["end_at"]),
            all_day=bool(event["all_day"]),
            description=event["description"] if isinstance(event["description"], str) else None,
            location=event["location"] if isinstance(event["location"], str) else None,
        )

    deleted = delete_stale_external_events(db, sub.url, seen_uids) if seen_uids else 0
    logger.info(
        "calendar.sync.done",
        url=sub.url,
        label=sub.label,
        upserted=len(events),
        deleted=deleted,
    )
    return len(events)


async def maybe_sync(db: sqlite3.Connection) -> None:
    """Sync configured ICS subscriptions if the interval has elapsed."""
    global _last_sync_at

    config = get_calendar_config()
    if config is None or not config.subscriptions:
        return

    now = now_ms()
    interval_ms = max(config.sync_interval_minutes, 0) * 60_000
    if interval_ms > 0 and now - _last_sync_at < interval_ms:
        return
    _last_sync_at = now

    for sub in config.subscriptions:
        try:
            await sync_subscription(db, sub)
        except Exception as exc:
            logger.error(
                "calendar.sync.error",
                url=sub.url,
                label=sub.label,
                error=get_error_message(exc),
            )


async def maybe_retry_hub_sync(db: sqlite3.Connection) -> None:
    """Retry pending hub calendar syncs from the scheduler tick."""
    if get_calendar_hub_config() is None:
        return

    pending = get_actions_pending_hub_sync(db, limit=5)
    for action in pending:
        try:
            if action.status == "cancelled" and action.hub_event_id:
                await delete_hub_event(db, action)
            elif action.hub_event_id:
                await update_hub_event(db, action)
            elif action.status not in ("cancelled", "failed"):
                await create_hub_event(db, action)
            else:
                clear_action_hub_event(db, action.id)
        except Exception as exc:
            logger.warn("hub.retry.error", action_id=action.id, error=get_error_message(exc))
            attempts = increment_hub_sync_attempts(db, action.id)
            if attempts >= _HUB_MAX_RETRIES:
                logger.warn("hub.retry.exhausted", action_id=action.id, attempts=attempts)
                mark_action_hub_failed(db, action.id)


def check_availability(
    db: sqlite3.Connection,
    start_ms: int,
    end_ms: int,
) -> tuple[bool, list[str]]:
    conflicts: list[str] = []
    for event in get_external_events_in_range(db, start_ms, end_ms):
        conflicts.append(event.title)
    for action in get_scheduled_actions_in_range(db, start_ms, end_ms):
        conflicts.append(action.intent)
    return len(conflicts) == 0, conflicts


def find_open_slots(
    db: sqlite3.Connection,
    start_ms: int,
    end_ms: int,
    *,
    min_duration_ms: int = 30 * 60_000,
) -> list[tuple[int, int]]:
    if end_ms <= start_ms:
        return []

    busy: list[tuple[int, int]] = []
    for event in get_external_events_in_range(db, start_ms, end_ms):
        busy.append((event.start_at, event.end_at))
    for action in get_scheduled_actions_in_range(db, start_ms, end_ms):
        if action.start_at is None or action.end_at is None:
            continue
        busy.append((action.start_at, action.end_at))

    busy.sort()
    merged: list[tuple[int, int]] = []
    for busy_start, busy_end in busy:
        if merged and busy_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], busy_end))
        else:
            merged.append((busy_start, busy_end))

    slots: list[tuple[int, int]] = []
    cursor = start_ms
    for busy_start, busy_end in merged:
        if busy_start - cursor >= min_duration_ms:
            slots.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
    if end_ms - cursor >= min_duration_ms:
        slots.append((cursor, end_ms))
    return slots


async def check_reminders(db: sqlite3.Connection) -> None:
    global _notified_ids

    config = get_calendar_config()
    if config is None:
        return

    if len(_notified_ids) > _MAX_NOTIFIED_CACHE:
        _notified_ids = set()

    from mystic.actions import notify

    tz = ZoneInfo(get_agent_config().hours.timezone)
    horizon_ms = max(config.reminder_minutes, 0) * 60_000

    for event in get_upcoming_external_events(db, within_ms=horizon_ms):
        if event.id in _notified_ids:
            continue
        await notify(
            f"Upcoming: {event.title}",
            f"Starts at {format_event_time(event.start_at, tz)}",
        )
        _notified_ids.add(event.id)

    for action in get_upcoming_scheduled_actions(db, within_ms=horizon_ms):
        if action.id in _notified_ids or action.start_at is None:
            continue
        await notify(
            f"Upcoming: {action.intent}",
            f"Starts at {format_event_time(action.start_at, tz)}",
        )
        _notified_ids.add(action.id)


async def _get_auth(hub: CalendarHubConfig) -> str:
    if hub.provider == "caldav":
        if not hub.username or not hub.password:
            raise RuntimeError("CalDAV credentials are missing from hub config.")
        return base64.b64encode(f"{hub.username}:{hub.password}".encode("utf-8")).decode("ascii")
    return await ensure_valid_token(hub)


async def _hub_call_with_retry(
    db: sqlite3.Connection,
    action: Action,
    dispatch: Mapping[str, HubCallFn],
    *,
    on_success: Callable[[sqlite3.Connection, Action, str | bool], None],
) -> bool:
    hub = get_calendar_hub_config()
    if hub is None:
        return False

    handler = dispatch.get(hub.provider)
    if handler is None:
        raise RuntimeError(f"Unsupported hub calendar provider: {hub.provider}")

    credential = await _get_auth(hub)
    try:
        result = await handler(hub, credential, action)
        on_success(db, action, result)
        return True
    except RuntimeError as exc:
        if "401" not in str(exc) or hub.provider == "caldav":
            raise
        credential = await ensure_valid_token(hub, force_refresh=True)
        result = await handler(hub, credential, action)
        on_success(db, action, result)
        return True


async def create_hub_event(db: sqlite3.Connection, action: Action) -> bool:
    if action.start_at is None or action.end_at is None:
        return False

    def on_success(_db: sqlite3.Connection, synced_action: Action, result: str | bool) -> None:
        if not isinstance(result, str):
            raise RuntimeError("Hub create did not return an event ID.")
        mark_action_hub_synced(_db, synced_action.id, result)

    return await _hub_call_with_retry(db, action, _HUB_CREATORS, on_success=on_success)


async def update_hub_event(db: sqlite3.Connection, action: Action) -> bool:
    if action.hub_event_id is None or action.start_at is None or action.end_at is None:
        return False

    def on_success(_db: sqlite3.Connection, synced_action: Action, _result: str | bool) -> None:
        mark_action_hub_synced(_db, synced_action.id, synced_action.hub_event_id or "")

    return await _hub_call_with_retry(db, action, _HUB_UPDATERS, on_success=on_success)


async def delete_hub_event(db: sqlite3.Connection, action: Action) -> bool:
    if action.hub_event_id is None:
        clear_action_hub_event(db, action.id)
        return True

    def on_success(_db: sqlite3.Connection, synced_action: Action, _result: str | bool) -> None:
        clear_action_hub_event(_db, synced_action.id)

    return await _hub_call_with_retry(db, action, _HUB_DELETERS, on_success=on_success)


async def _google_create_event(hub: CalendarHubConfig, token: str, action: Action) -> str:
    tz = get_agent_config().hours.timezone
    body: dict[str, object] = {
        "summary": action.intent,
        "start": {"dateTime": _ms_to_rfc3339(action.start_at, tz)},
        "end": {"dateTime": _ms_to_rfc3339(action.end_at, tz)},
    }
    if action.context:
        body["description"] = action.context

    calendar_id = quote(hub.calendar_id, safe="")
    response = await fetch_with_timeout(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        json_body=body,
        timeout_ms=10_000,
        timeout_label="google-calendar-create",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Google Calendar create failed: HTTP {response.status_code}")
    return str(response.json()["id"])


async def _google_update_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        raise RuntimeError("Hub event ID required for Google update.")
    tz = get_agent_config().hours.timezone
    body: dict[str, object] = {
        "summary": action.intent,
        "start": {"dateTime": _ms_to_rfc3339(action.start_at, tz)},
        "end": {"dateTime": _ms_to_rfc3339(action.end_at, tz)},
    }
    if action.context:
        body["description"] = action.context

    calendar_id = quote(hub.calendar_id, safe="")
    event_id = quote(action.hub_event_id, safe="")
    response = await fetch_with_timeout(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        method="PATCH",
        headers={"Authorization": f"Bearer {token}"},
        json_body=body,
        timeout_ms=10_000,
        timeout_label="google-calendar-update",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Google Calendar update failed: HTTP {response.status_code}")
    return True


async def _google_delete_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        return True
    calendar_id = quote(hub.calendar_id, safe="")
    event_id = quote(action.hub_event_id, safe="")
    response = await fetch_with_timeout(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
        timeout_ms=10_000,
        timeout_label="google-calendar-delete",
    )
    if response.status_code in (404, 410):
        return True
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Google Calendar delete failed: HTTP {response.status_code}")
    return True


async def _microsoft_create_event(hub: CalendarHubConfig, token: str, action: Action) -> str:
    tz = get_agent_config().hours.timezone
    body: dict[str, object] = {
        "subject": action.intent,
        "start": {"dateTime": _ms_to_graph_datetime(action.start_at), "timeZone": tz},
        "end": {"dateTime": _ms_to_graph_datetime(action.end_at), "timeZone": tz},
    }
    if action.context:
        body["body"] = {"contentType": "text", "content": action.context}

    calendar_id = quote(hub.calendar_id, safe="")
    response = await fetch_with_timeout(
        f"https://graph.microsoft.com/v1.0/me/calendars/{calendar_id}/events",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        json_body=body,
        timeout_ms=10_000,
        timeout_label="microsoft-calendar-create",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Microsoft Calendar create failed: HTTP {response.status_code}")
    return str(response.json()["id"])


async def _microsoft_update_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        raise RuntimeError("Hub event ID required for Microsoft update.")
    tz = get_agent_config().hours.timezone
    body: dict[str, object] = {
        "subject": action.intent,
        "start": {"dateTime": _ms_to_graph_datetime(action.start_at), "timeZone": tz},
        "end": {"dateTime": _ms_to_graph_datetime(action.end_at), "timeZone": tz},
    }
    if action.context:
        body["body"] = {"contentType": "text", "content": action.context}

    calendar_id = quote(hub.calendar_id, safe="")
    event_id = quote(action.hub_event_id, safe="")
    response = await fetch_with_timeout(
        f"https://graph.microsoft.com/v1.0/me/calendars/{calendar_id}/events/{event_id}",
        method="PATCH",
        headers={"Authorization": f"Bearer {token}"},
        json_body=body,
        timeout_ms=10_000,
        timeout_label="microsoft-calendar-update",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Microsoft Calendar update failed: HTTP {response.status_code}")
    return True


async def _microsoft_delete_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        return True
    calendar_id = quote(hub.calendar_id, safe="")
    event_id = quote(action.hub_event_id, safe="")
    response = await fetch_with_timeout(
        f"https://graph.microsoft.com/v1.0/me/calendars/{calendar_id}/events/{event_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
        timeout_ms=10_000,
        timeout_label="microsoft-calendar-delete",
    )
    if response.status_code in (404, 410):
        return True
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Microsoft Calendar delete failed: HTTP {response.status_code}")
    return True


async def _caldav_create_event(hub: CalendarHubConfig, token: str, action: Action) -> str:
    uid = str(uuid.uuid4())
    response = await fetch_with_timeout(
        _caldav_event_url(hub, uid),
        method="PUT",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "text/calendar; charset=utf-8",
        },
        data=_build_vevent(uid, action),
        timeout_ms=10_000,
        timeout_label="caldav-create",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"CalDAV create failed: HTTP {response.status_code}")
    return uid


async def _caldav_update_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        raise RuntimeError("Hub event ID required for CalDAV update.")
    response = await fetch_with_timeout(
        _caldav_event_url(hub, action.hub_event_id),
        method="PUT",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "text/calendar; charset=utf-8",
        },
        data=_build_vevent(action.hub_event_id, action),
        timeout_ms=10_000,
        timeout_label="caldav-update",
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"CalDAV update failed: HTTP {response.status_code}")
    return True


async def _caldav_delete_event(hub: CalendarHubConfig, token: str, action: Action) -> bool:
    if action.hub_event_id is None:
        return True
    response = await fetch_with_timeout(
        _caldav_event_url(hub, action.hub_event_id),
        method="DELETE",
        headers={"Authorization": f"Basic {token}"},
        timeout_ms=10_000,
        timeout_label="caldav-delete",
    )
    if response.status_code in (404, 410):
        return True
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"CalDAV delete failed: HTTP {response.status_code}")
    return True


_HUB_CREATORS: dict[str, HubCallFn] = {
    "google": _google_create_event,
    "microsoft": _microsoft_create_event,
    "caldav": _caldav_create_event,
}
_HUB_UPDATERS: dict[str, HubCallFn] = {
    "google": _google_update_event,
    "microsoft": _microsoft_update_event,
    "caldav": _caldav_update_event,
}
_HUB_DELETERS: dict[str, HubCallFn] = {
    "google": _google_delete_event,
    "microsoft": _microsoft_delete_event,
    "caldav": _caldav_delete_event,
}


def _date_to_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=UTC).timestamp() * 1000)


def _ensure_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_schedule_start(timestamp_ms: int, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=tz)
    return f"{dt.strftime('%a %b')} {dt.day} at {dt.strftime('%I:%M %p')}"


def _ms_to_rfc3339(timestamp_ms: int | None, timezone: str) -> str:
    if timestamp_ms is None:
        raise ValueError("Timestamp required for hub event.")
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=ZoneInfo(timezone))
    return dt.isoformat()


def _ms_to_graph_datetime(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        raise ValueError("Timestamp required for hub event.")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _ms_to_ical_utc(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        raise ValueError("Timestamp required for hub event.")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _build_vevent(uid: str, action: Action) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mystic Horizon//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_ms_to_ical_utc(now_ms())}",
        f"DTSTART:{_ms_to_ical_utc(action.start_at)}",
        f"DTEND:{_ms_to_ical_utc(action.end_at)}",
        f"SUMMARY:{_escape_ics_text(action.intent)}",
    ]
    if action.context:
        lines.append(f"DESCRIPTION:{_escape_ics_text(action.context)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines).encode("utf-8")


def _caldav_event_url(hub: CalendarHubConfig, event_id: str) -> str:
    if not hub.base_url:
        raise RuntimeError("CalDAV base URL is missing from hub config.")
    base = hub.base_url.rstrip("/")
    calendar_path = hub.calendar_id.strip("/")
    return f"{base}/{calendar_path}/{quote(event_id, safe='')}.ics"
