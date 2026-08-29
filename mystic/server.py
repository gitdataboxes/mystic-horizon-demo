"""aiohttp application: rate limit, media auth, webhooks, and app setup."""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from aiohttp import WSMsgType, web

from mystic.audio import start_call_recorder
from mystic.config import (
    bind_trace_id,
    get_error_message,
    get_providers_config,
    get_tunnel_url,
    logger,
)
from mystic.db import get_call_by_id, now_ms as _now_ms
from mystic.livekit import create_audio_bridge
from mystic.web import register_dashboard_routes

# ── rate limit ───────────────────────────────────────────────────────────────

DEFAULT_LIMIT = 60
WINDOW_MS = 60 * 1000


@dataclass(slots=True)
class RateLimitEntry:
    count: int
    reset_at: int


_store: dict[str, RateLimitEntry] = {}


def check_rate_limit(
    request: object,
    limit: int = DEFAULT_LIMIT,
    *,
    now_ms: int | None = None,
) -> web.Response | None:
    current_ms = now_ms if now_ms is not None else _now_ms()
    _prune_expired_entries(current_ms)

    ip = _get_request_ip(request)
    entry = _store.get(ip)
    if entry is None or current_ms > entry.reset_at:
        entry = RateLimitEntry(count=0, reset_at=current_ms + WINDOW_MS)
        _store[ip] = entry

    entry.count += 1
    if entry.count > limit:
        return web.json_response({"error": "Too many requests"}, status=429)
    return None


def clear_rate_limit_store() -> None:
    _store.clear()


def _prune_expired_entries(now_ms: int) -> None:
    expired = [key for key, entry in _store.items() if now_ms > entry.reset_at]
    for key in expired:
        _store.pop(key, None)


def _get_request_ip(request: object) -> str:
    headers = getattr(request, "headers", {}) or {}
    if not isinstance(headers, dict):
        try:
            forwarded_for = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
            real_ip = headers.get("X-Real-Ip") or headers.get("x-real-ip")
        except AttributeError:
            forwarded_for = None
            real_ip = None
    else:
        forwarded_for = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        real_ip = headers.get("X-Real-Ip") or headers.get("x-real-ip")

    if isinstance(forwarded_for, str) and forwarded_for.strip():
        return forwarded_for.split(",")[0].strip()
    if isinstance(real_ip, str) and real_ip.strip():
        return real_ip.strip()

    remote = getattr(request, "remote", None)
    return remote if isinstance(remote, str) and remote else "unknown"


# ── media auth ───────────────────────────────────────────────────────────────

MEDIA_STREAM_TOKEN_TTL_MS = 15 * 60 * 1000


def build_authenticated_media_stream_url(
    tunnel_url: str,
    call_id: str,
    secret: str,
    *,
    now_ms: int | None = None,
) -> str:
    parsed = urlparse(tunnel_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid tunnel URL: {tunnel_url}")

    issued_at = now_ms if now_ms is not None else _now_ms()
    expires_at = issued_at + MEDIA_STREAM_TOKEN_TTL_MS
    token = _encode_signature(call_id, expires_at, secret)
    return urlunparse(
        (
            "wss",
            parsed.netloc,
            "/media-stream",
            "",
            urlencode(
                {
                    "callId": call_id,
                    "expiresAt": str(expires_at),
                    "token": token,
                }
            ),
            "",
        )
    )


def validate_media_stream_request(
    url: object,
    secret: str,
    *,
    now_ms: int | None = None,
) -> str | None:
    parsed = urlparse(str(url))
    params = parse_qs(parsed.query)
    call_id = _first_value(params.get("callId"))
    token = _first_value(params.get("token"))
    expires_at_raw = _first_value(params.get("expiresAt"))
    if not call_id or not token or not expires_at_raw:
        return None

    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return None

    current_ms = now_ms if now_ms is not None else _now_ms()
    if expires_at <= current_ms:
        return None

    expected = _encode_signature(call_id, expires_at, secret)
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return None
    return call_id


def _encode_signature(call_id: str, expires_at: int, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{call_id}.{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _first_value(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


# ── webhooks ─────────────────────────────────────────────────────────────────


def _parse_form_body(body: str) -> dict[str, str]:
    return {key: value for key, value in parse_qsl(body, keep_blank_values=True)}


def _json_response(payload: Mapping[str, object], status: int = 200) -> web.Response:
    return web.json_response(dict(payload), status=status)


def _xml_response(payload: str) -> web.Response:
    return web.Response(text=payload, status=200, content_type="application/xml")


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(slots=True)
class WebhookHandler:
    db: sqlite3.Connection
    tunnel_url: str

    def _current_tunnel_url(self) -> str:
        if self.tunnel_url.startswith(("http://localhost", "http://127.0.0.1")):
            return get_tunnel_url() or self.tunnel_url
        return self.tunnel_url

    async def voice(self, request: object) -> web.Response:
        from mystic.calls import handle_incoming_call, validate_twilio_signature

        params = _parse_form_body(await _request_text(request))
        providers_config = get_providers_config()
        if providers_config.twilio is None:
            return _json_response({"error": "Twilio not configured"}, status=503)

        signature = _request_header(request, "X-Twilio-Signature")
        tunnel_url = self._current_tunnel_url()
        request_url = f"{tunnel_url}/webhook/twilio/voice"
        if not validate_twilio_signature(providers_config.twilio, signature, request_url, params):
            logger.warn("webhook.twilio.invalid-signature")
            return _json_response({"error": "Unauthorized"}, status=401)

        call_sid = params.get("CallSid")
        if not call_sid:
            return _json_response({"error": "Missing CallSid"}, status=400)

        result = await handle_incoming_call(
            self.db,
            params.get("From"),
            call_sid,
            tunnel_url,
        )
        if "error" in result:
            return _json_response({"error": result["error"]}, status=result["status"])
        return _xml_response(result["twiml"])

    async def status(self, request: object) -> web.Response:
        from mystic.calls import (
            clear_transfer_target_sid,
            handle_answered_outbound,
            handle_completed_call_status,
            handle_unanswered_outbound,
            reconnect_call_to_stream,
            update_live_call,
            validate_twilio_signature,
        )

        params = _parse_form_body(await _request_text(request))
        providers_config = get_providers_config()
        if providers_config.twilio is None:
            return _json_response({"error": "Twilio not configured"}, status=503)

        signature = _request_header(request, "X-Twilio-Signature")
        tunnel_url = self._current_tunnel_url()
        request_url = _build_request_url(tunnel_url, "/webhook/twilio/status", request)
        if not validate_twilio_signature(providers_config.twilio, signature, request_url, params):
            logger.warn("webhook.twilio.status.invalid-signature")
            return _json_response({"error": "Unauthorized"}, status=401)

        call_sid = params.get("CallSid")
        call_status = params.get("CallStatus")
        if not call_sid or not call_status:
            return _json_response({"error": "Missing CallSid or CallStatus"}, status=400)

        caller_call_id = _request_query_param(request, "callerCallId")
        logger.debug(
            "webhook.twilio.status",
            callSid=call_sid,
            status=call_status,
            callerCallId=caller_call_id,
        )
        if caller_call_id:
            if call_status in {"no-answer", "busy", "failed", "canceled"}:
                caller_call = get_call_by_id(self.db, caller_call_id)
                stream_twiml = await reconnect_call_to_stream(self.db, caller_call_id, tunnel_url)
                if (
                    caller_call is not None
                    and caller_call.external_id
                    and stream_twiml is not None
                ):
                    await update_live_call(
                        providers_config.twilio,
                        caller_call.external_id,
                        twiml=stream_twiml,
                    )
                    logger.info(
                        "webhook.twilio.status.transfer-failure.reconnected",
                        callerCallId=caller_call_id,
                        targetCallSid=call_sid,
                        status=call_status,
                    )
                clear_transfer_target_sid(caller_call_id)
                return _json_response({"ok": True})
            if call_status == "completed":
                clear_transfer_target_sid(caller_call_id)
                return _json_response({"ok": True})
            if call_status == "answered":
                return _json_response({"ok": True})

        if call_status == "answered":
            handle_answered_outbound(self.db, call_sid)
        elif call_status == "completed":
            duration = _parse_optional_int(params.get("CallDuration"))
            await handle_completed_call_status(self.db, call_sid, duration)
            logger.info("webhook.twilio.completed", callSid=call_sid, duration=duration)
        elif call_status in {"no-answer", "busy", "failed", "canceled"}:
            handle_unanswered_outbound(self.db, call_sid)

        return _json_response({"ok": True})


    async def dial_action(self, request: object) -> web.Response:
        from mystic.calls import reconnect_call_to_stream, validate_twilio_signature

        params = _parse_form_body(await _request_text(request))
        providers_config = get_providers_config()
        if providers_config.twilio is None:
            return _xml_response(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Something went wrong.</Say><Hangup/></Response>"
            )

        call_id = _request_query_param(request, "callId") or ""
        reconnect = _request_query_param(request, "reconnect") == "1"
        tunnel_url = self._current_tunnel_url()
        request_url = _build_request_url(tunnel_url, "/webhook/twilio/dial-action", request)

        signature = _request_header(request, "X-Twilio-Signature")
        if not validate_twilio_signature(providers_config.twilio, signature, request_url, params):
            logger.warn("webhook.twilio.dial-action.invalid-signature")
            return _json_response({"error": "Unauthorized"}, status=401)

        dial_status = params.get("DialCallStatus", "")
        logger.info(
            "webhook.twilio.dial-action",
            callId=call_id,
            dialStatus=dial_status,
            reconnect=reconnect,
        )

        if dial_status == "completed" and not reconnect:
            return _xml_response(
                '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
            )

        if not call_id:
            return _xml_response(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Something went wrong.</Say><Hangup/></Response>"
            )

        reconnect_statuses = {"completed", "no-answer", "busy", "failed", "canceled"}
        if dial_status not in reconnect_statuses:
            return _xml_response(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Something went wrong.</Say><Hangup/></Response>"
            )

        stream_twiml = await reconnect_call_to_stream(self.db, call_id, tunnel_url)
        if stream_twiml is None:
            return _xml_response(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Something went wrong.</Say><Hangup/></Response>"
            )

        # Inline composition: prepend <Say> to the stream TwiML's <Connect> content
        connect_start = stream_twiml.find("<Connect>")
        if connect_start == -1:
            return _xml_response(stream_twiml)
        reconnect_message = (
            "Let me reconnect you."
            if dial_status == "completed"
            else (
                "I'm sorry, they didn't answer. Let me reconnect you."
                if dial_status == "no-answer"
                else "That transfer did not go through. Let me reconnect you."
            )
        )
        return _xml_response(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"<Say>{reconnect_message}</Say>"
            f"{stream_twiml[connect_start:stream_twiml.rfind('</Response>')]}"
            "</Response>"
        )


def create_webhook_handler(db: sqlite3.Connection, tunnel_url: str) -> WebhookHandler:
    return WebhookHandler(db=db, tunnel_url=tunnel_url)


async def _request_text(request: object) -> str:
    text = getattr(request, "text", None)
    if text is None or not callable(text):
        raise TypeError("Request object does not expose an async text() method")
    return str(await cast(Any, text)())


def _request_header(request: object, key: str) -> str:
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None or not callable(getter):
        return ""
    value = cast(Any, getter)(key, "")
    return value if isinstance(value, str) else ""


def _request_query_param(request: object, key: str) -> str | None:
    query = getattr(request, "query", None)
    if query is None:
        return None
    getter = getattr(query, "get", None)
    if getter is None or not callable(getter):
        return None
    value = cast(Any, getter)(key)
    return value if isinstance(value, str) else None


def _request_query_items(request: object) -> list[tuple[str, str]]:
    query = getattr(request, "query", None)
    if query is None:
        return []
    items = getattr(query, "items", None)
    if items is None or not callable(items):
        return []
    return [
        (str(key), str(value))
        for key, value in cast(Any, items)()
        if isinstance(key, str) and isinstance(value, str)
    ]


def _build_request_url(tunnel_url: str, path: str, request: object) -> str:
    query_items = _request_query_items(request)
    if not query_items:
        return f"{tunnel_url}{path}"
    return f"{tunnel_url}{path}?{urlencode(query_items)}"


# ── app ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RunningServer:
    runner: web.AppRunner
    host: str
    port: int

    async def close(self) -> None:
        await self.runner.cleanup()


def create_app(db: sqlite3.Connection, tunnel_url: str) -> web.Application:
    app = web.Application()
    webhook_handler = create_webhook_handler(db, tunnel_url)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "timestamp": _now_ms()})

    async def voice(request: web.Request) -> web.Response:
        limited = check_rate_limit(request)
        if limited is not None:
            return limited
        return await webhook_handler.voice(request)

    async def status(request: web.Request) -> web.Response:
        limited = check_rate_limit(request)
        if limited is not None:
            return limited
        return await webhook_handler.status(request)

    async def media_stream(request: web.Request) -> web.StreamResponse:
        providers_config = get_providers_config()
        if providers_config.twilio is None:
            return web.Response(status=503, text="Twilio not configured")

        call_id = validate_media_stream_request(request.url, providers_config.twilio.authToken)
        if call_id is None:
            logger.warn("media-stream.unauthorized")
            return web.Response(status=401, text="Unauthorized")
        bind_trace_id(call_id)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        bridge = create_audio_bridge(
            ws,
            livekit_config=providers_config.livekit,
            call_id=call_id,
            recorder=start_call_recorder(call_id),
        )
        try:
            await bridge.start()
            logger.info("media-stream.connected", callId=call_id)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await bridge.handle_twilio_message(message.data)
                elif message.type == WSMsgType.ERROR:
                    logger.error(
                        "media-stream.ws.error",
                        callId=call_id,
                        error=get_error_message(ws.exception() or RuntimeError("websocket error")),
                    )
                    break
        except Exception as exc:
            logger.error(
                "media-stream.bridge.error",
                callId=call_id,
                error=get_error_message(exc),
            )
            with suppress(Exception):
                await ws.close(code=1011, message=b"Bridge error")
        finally:
            with suppress(Exception):
                await bridge.stop()
            logger.debug("media-stream.closed", callId=call_id)

        return ws

    async def dial_action(request: web.Request) -> web.Response:
        limited = check_rate_limit(request)
        if limited is not None:
            return limited
        return await webhook_handler.dial_action(request)

    app.router.add_get("/health", health)
    app.router.add_post("/webhook/twilio/voice", voice)
    app.router.add_post("/webhook/twilio/status", status)
    app.router.add_post("/webhook/twilio/dial-action", dial_action)
    app.router.add_get("/media-stream", media_stream)
    register_dashboard_routes(app, db)
    return app


async def start_server(
    app: web.Application,
    port: int,
    *,
    host: str = "0.0.0.0",
    shutdown_timeout: float = 60.0,
) -> RunningServer:
    runner = web.AppRunner(app, shutdown_timeout=shutdown_timeout)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise

    bound_port = _resolve_bound_port(site, port)
    logger.info("server.started", host=host, port=bound_port)
    return RunningServer(runner=runner, host=host, port=bound_port)


def _resolve_bound_port(site: web.TCPSite, default_port: int) -> int:
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None)
    if not sockets:
        return default_port
    first_socket = sockets[0]
    address = first_socket.getsockname()
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int):
        return address[1]
    return default_port
