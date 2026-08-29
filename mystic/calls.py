"""Twilio+TwiML client, call state, context, end-of-call, and initiation helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import urlencode
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from mystic.actions import check_satisfaction, finalize_in_progress_action, start_action_attempt
from mystic.config import (
    LiveKitConfig,
    TtsConfig,
    TwilioConfig,
    emit_event,
    get_agent_config,
    get_error_message,
    get_providers_config,
    get_tunnel_url,
    logger,
)
from mystic.db import (
    clear_active_calls as _clear_persisted_active_calls,
    count_active_calls as _count_persisted_active_calls,
    delete_active_call,
    delete_call_by_id,
    get_active_call_by_id,
    get_call_by_external_id,
    get_call_by_id,
    get_pending_actions_by_person,
    get_person_by_id,
    get_recent_calls_by_person,
    insert_call,
    list_active_calls,
    mark_extraction_attempted,
    now_ms,
    sweep_timed_out_active_calls,
    touch_active_call as _touch_persisted_active_call,
    update_active_call_started_at as _update_persisted_active_call_started_at,
    update_call_answered_at,
    update_call_end,
    update_call_external_id,
    update_person_last_seen,
    upsert_active_call,
    upsert_person,
)
from mystic.livekit import create_room, delete_room
from mystic.memory import hybrid_search
from mystic.prompts import build_prompt, compute_variables
from mystic.server import build_authenticated_media_stream_url
from mystic.http import HttpResponse, RequestTransport, fetch_with_timeout
from mystic.interactions import describe_call, describe_interaction, interaction_event_payload
from mystic.types import Action, Audience, Call, CallState, Channel, Direction, InteractionModality, Person

# ── TwiML builders ──


def _escape_xml(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _build_stream_connect_xml(ws_url: str, params: Mapping[str, str] | None = None) -> str:
    param_entries = ""
    if params:
        param_entries = "".join(
            f'<Parameter name="{_escape_xml(key)}" value="{_escape_xml(value)}" />'
            for key, value in params.items()
        )

    return "".join(
        (
            "<Connect>",
            f'<Stream url="{_escape_xml(ws_url)}">',
            param_entries,
            "</Stream>",
            "</Connect>",
        )
    )


def generate_stream_twiml(ws_url: str, params: Mapping[str, str] | None = None) -> str:
    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            _build_stream_connect_xml(ws_url, params),
            "</Response>",
        )
    )


def generate_say_twiml(message: str) -> str:
    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f"<Say>{_escape_xml(message)}</Say>",
            "</Response>",
        )
    )


def generate_dial_twiml(
    destination: str,
    *,
    caller_id: str | None = None,
    timeout: int = 30,
    action: str | None = None,
) -> str:
    attributes = [f'timeout="{timeout}"']
    if caller_id is not None:
        attributes.insert(0, f'callerId="{_escape_xml(caller_id)}"')
    if action is not None:
        attributes.append(f'action="{_escape_xml(action)}"')

    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f'<Dial {" ".join(attributes)}>{_escape_xml(destination)}</Dial>',
            "</Response>",
        )
    )


def generate_hold_twiml(
    message: str = "Please hold.",
    *,
    loops: int = 10,
    resume_ws_url: str | None = None,
    resume_params: Mapping[str, str] | None = None,
) -> str:
    loop_count = loops if resume_ws_url is not None else 0
    reconnect_xml = (
        _build_stream_connect_xml(resume_ws_url, resume_params)
        if resume_ws_url is not None
        else ""
    )
    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f'<Say loop="{loop_count}">{_escape_xml(message)}</Say>',
            reconnect_xml,
            "</Response>",
        )
    )


def _build_conference_dial_xml(
    conference_name: str,
    *,
    start_on_enter: bool = True,
    end_on_exit: bool = False,
    beep: bool = False,
    action: str | None = None,
) -> str:
    dial_attributes: list[str] = []
    if action is not None:
        dial_attributes.append(f'action="{_escape_xml(action)}"')
    conference_attributes = [
        f'startConferenceOnEnter="{"true" if start_on_enter else "false"}"',
        f'endConferenceOnExit="{"true" if end_on_exit else "false"}"',
        f'beep="{"true" if beep else "false"}"',
    ]
    dial_open = f'<Dial {" ".join(dial_attributes)}>' if dial_attributes else "<Dial>"
    return "".join(
        (
            dial_open,
            f'<Conference {" ".join(conference_attributes)}>{_escape_xml(conference_name)}</Conference>',
            "</Dial>",
        )
    )


def generate_conference_twiml(
    conference_name: str,
    *,
    start_on_enter: bool = True,
    end_on_exit: bool = False,
    beep: bool = False,
    action: str | None = None,
) -> str:
    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            _build_conference_dial_xml(
                conference_name,
                start_on_enter=start_on_enter,
                end_on_exit=end_on_exit,
                beep=beep,
                action=action,
            ),
            "</Response>",
        )
    )


def generate_say_conference_twiml(
    message: str,
    conference_name: str,
    **conference_kwargs: Any,
) -> str:
    return "".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f"<Say>{_escape_xml(message)}</Say>",
            _build_conference_dial_xml(conference_name, **conference_kwargs),
            "</Response>",
        )
    )


# ── Twilio REST client ──

TWILIO_API = "https://api.twilio.com/2010-04-01"
_TWILIO_TIMEOUT_MS = 15_000


class AvailablePhoneNumber(TypedDict):
    phoneNumber: str
    friendlyName: str


class PurchasedPhoneNumber(TypedDict):
    sid: str
    phoneNumber: str


class OwnedPhoneNumber(TypedDict):
    sid: str
    phoneNumber: str
    friendlyName: str


class IncomingPhoneNumber(TypedDict):
    sid: str
    phoneNumber: str
    friendlyName: str
    voiceUrl: str
    statusCallback: str


def _auth_headers(config: TwilioConfig) -> dict[str, str]:
    credentials = base64.b64encode(
        f"{config.accountSid}:{config.authToken}".encode("utf-8")
    ).decode("utf-8")
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _encode_params(params: Mapping[str, str | Sequence[str]]) -> bytes:
    return urlencode(params, doseq=True).encode("utf-8")


def _require_json_response(response: object, action: str) -> dict[str, object]:
    if not isinstance(response, HttpResponse):
        raise TypeError("fetch_with_timeout returned an unexpected response type")
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Twilio {action} failed: {response.status_code} {response.text}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Twilio {action} response was not a JSON object")
    return cast(dict[str, object], payload)


async def search_available_numbers(
    config: TwilioConfig,
    *,
    country: str = "US",
    area_code: str | None = None,
    transport: RequestTransport | None = None,
) -> list[AvailablePhoneNumber]:
    query_params: dict[str, str] = {"PageSize": "5"}
    if area_code is not None:
        query_params["AreaCode"] = area_code

    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/AvailablePhoneNumbers/{country}/Local.json"
        f"?{urlencode(query_params)}",
        headers=_auth_headers(config),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.search",
        transport=transport,
    )
    payload = _require_json_response(response, "search")
    numbers_raw = payload.get("available_phone_numbers")
    if not isinstance(numbers_raw, list):
        raise RuntimeError("Twilio search response did not include available_phone_numbers")

    numbers_list = cast(list[object], numbers_raw)
    numbers: list[AvailablePhoneNumber] = []
    for item in numbers_list:
        if not isinstance(item, dict):
            continue
        item_payload = cast(dict[str, object], item)
        phone_number = item_payload.get("phone_number")
        friendly_name = item_payload.get("friendly_name")
        if isinstance(phone_number, str) and isinstance(friendly_name, str):
            numbers.append(
                {
                    "phoneNumber": phone_number,
                    "friendlyName": friendly_name,
                }
            )
    return numbers


async def list_incoming_phone_numbers(
    config: TwilioConfig,
    *,
    transport: RequestTransport | None = None,
) -> list[OwnedPhoneNumber]:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/IncomingPhoneNumbers.json"
        f"?{urlencode({'PageSize': '50'})}",
        headers=_auth_headers(config),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.list-incoming",
        transport=transport,
    )
    payload = _require_json_response(response, "list-incoming")
    numbers_raw = payload.get("incoming_phone_numbers")
    if not isinstance(numbers_raw, list):
        raise RuntimeError("Twilio list-incoming response did not include incoming_phone_numbers")

    numbers_list = cast(list[object], numbers_raw)
    numbers: list[OwnedPhoneNumber] = []
    for item in numbers_list:
        if not isinstance(item, dict):
            continue
        item_payload = cast(dict[str, object], item)
        phone_number = item_payload.get("phone_number")
        friendly_name = item_payload.get("friendly_name")
        sid = item_payload.get("sid")
        if (
            isinstance(phone_number, str)
            and isinstance(friendly_name, str)
            and isinstance(sid, str)
        ):
            numbers.append(
                {
                    "sid": sid,
                    "phoneNumber": phone_number,
                    "friendlyName": friendly_name,
                }
            )
    return numbers


async def get_incoming_phone_number(
    config: TwilioConfig,
    number_sid: str,
    *,
    transport: RequestTransport | None = None,
) -> IncomingPhoneNumber:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/IncomingPhoneNumbers/{number_sid}.json",
        headers=_auth_headers(config),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.get-incoming",
        transport=transport,
    )
    payload = _require_json_response(response, "get-incoming")
    sid = payload.get("sid")
    phone_number = payload.get("phone_number")
    friendly_name = payload.get("friendly_name")
    voice_url = payload.get("voice_url")
    status_callback = payload.get("status_callback")
    if not isinstance(sid, str) or not isinstance(phone_number, str):
        raise RuntimeError("Twilio get-incoming response did not include sid and phone_number")
    return {
        "sid": sid,
        "phoneNumber": phone_number,
        "friendlyName": friendly_name if isinstance(friendly_name, str) else "",
        "voiceUrl": voice_url if isinstance(voice_url, str) else "",
        "statusCallback": status_callback if isinstance(status_callback, str) else "",
    }


async def buy_phone_number(
    config: TwilioConfig,
    phone_number: str,
    voice_url: str,
    status_url: str,
    *,
    transport: RequestTransport | None = None,
) -> PurchasedPhoneNumber:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/IncomingPhoneNumbers.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params(
            {
                "PhoneNumber": phone_number,
                "VoiceUrl": voice_url,
                "VoiceMethod": "POST",
                "StatusCallback": status_url,
                "StatusCallbackMethod": "POST",
            }
        ),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.buy-number",
        transport=transport,
    )
    payload = _require_json_response(response, "buy")
    sid = payload.get("sid")
    response_number = payload.get("phone_number")
    if not isinstance(sid, str) or not isinstance(response_number, str):
        raise RuntimeError("Twilio buy response did not include sid and phone_number")

    logger.info("twilio.number.purchased", sid=sid, number=response_number)
    return {"sid": sid, "phoneNumber": response_number}


async def update_phone_webhook(
    config: TwilioConfig,
    number_sid: str,
    voice_url: str,
    status_url: str,
    *,
    transport: RequestTransport | None = None,
) -> None:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/IncomingPhoneNumbers/{number_sid}.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params(
            {
                "VoiceUrl": voice_url,
                "VoiceMethod": "POST",
                "StatusCallback": status_url,
                "StatusCallbackMethod": "POST",
            }
        ),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.update-webhook",
        transport=transport,
    )
    _require_json_response(response, "update")
    logger.info("twilio.webhook.updated", numberSid=number_sid, voiceUrl=voice_url)


async def make_outbound_call(
    config: TwilioConfig,
    to: str,
    twiml: str,
    status_callback: str,
    *,
    transport: RequestTransport | None = None,
) -> str:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/Calls.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params(
            {
                "From": config.phoneNumber,
                "To": to,
                "Twiml": twiml,
                "StatusCallback": status_callback,
                "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                "StatusCallbackMethod": "POST",
            }
        ),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.make-call",
        transport=transport,
    )
    payload = _require_json_response(response, "call")
    sid = payload.get("sid")
    if not isinstance(sid, str) or not sid:
        raise RuntimeError("Twilio response did not include a call sid")

    logger.info("twilio.call.initiated", callSid=sid, to=to)
    return sid


async def send_sms(
    config: TwilioConfig,
    to: str,
    body: str,
    *,
    transport: RequestTransport | None = None,
) -> str:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/Messages.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params({"From": config.phoneNumber, "To": to, "Body": body}),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.send-sms",
        transport=transport,
    )
    payload = _require_json_response(response, "sms")
    sid = payload.get("sid")
    if not isinstance(sid, str) or not sid:
        raise RuntimeError("Twilio response did not include a message sid")
    logger.info("twilio.sms.sent", messageSid=sid, to=to)
    return sid


async def end_call(
    config: TwilioConfig,
    call_sid: str,
    *,
    transport: RequestTransport | None = None,
) -> None:
    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/Calls/{call_sid}.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params({"Status": "completed"}),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.end-call",
        transport=transport,
    )
    if not isinstance(response, HttpResponse):
        return
    if not 200 <= response.status_code < 300:
        logger.warn("twilio.call.end.failed", callSid=call_sid, status=response.status_code)


async def update_live_call(
    config: TwilioConfig,
    call_sid: str,
    *,
    twiml: str | None = None,
    url: str | None = None,
    transport: RequestTransport | None = None,
) -> None:
    if twiml is None and url is None:
        raise ValueError("update_live_call requires twiml or url")

    params: dict[str, str] = {}
    if twiml is not None:
        params["Twiml"] = twiml
    if url is not None:
        params["Url"] = url

    response = await fetch_with_timeout(
        f"{TWILIO_API}/Accounts/{config.accountSid}/Calls/{call_sid}.json",
        method="POST",
        headers=_auth_headers(config),
        data=_encode_params(params),
        timeout_ms=_TWILIO_TIMEOUT_MS,
        timeout_label="twilio.update-call",
        transport=transport,
    )
    if not isinstance(response, HttpResponse):
        return
    if not 200 <= response.status_code < 300:
        logger.warn("twilio.call.update.failed", callSid=call_sid, status=response.status_code)
        return

    logger.info(
        "twilio.call.updated",
        callSid=call_sid,
        hasTwiml=twiml is not None,
        hasUrl=url is not None,
    )


def validate_twilio_signature(
    config: TwilioConfig,
    signature: str,
    url: str,
    params: Mapping[str, str],
) -> bool:
    sorted_data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    expected = base64.b64encode(
        hmac.new(
            config.authToken.encode("utf-8"),
            sorted_data.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8"))


# ── active call tracking ──

OUTBOUND_TIMEOUT_MS = 5 * 60 * 1000
_active_calls: dict[str, CallState] = {}
_transfer_target_sids: dict[str, str] = {}


def add_active_call(state: CallState, db: sqlite3.Connection | None = None) -> None:
    if db is not None:
        upsert_active_call(db, state)
    _active_calls[state.call_id] = state
    logger.debug(
        "active-calls.add",
        callId=state.call_id,
        direction=state.direction,
        person=state.person_name,
    )


def remove_active_call(
    call_id: str,
    db: sqlite3.Connection | None = None,
) -> CallState | None:
    state = delete_active_call(db, call_id) if db is not None else _active_calls.get(call_id)
    if state is None:
        state = _active_calls.get(call_id)
    _active_calls.pop(call_id, None)
    if state is not None:
        logger.debug("active-calls.remove", callId=call_id, person=state.person_name)
    return state


def get_active_call(call_id: str, db: sqlite3.Connection | None = None) -> CallState | None:
    if db is not None:
        return get_active_call_by_id(db, call_id)
    return _active_calls.get(call_id)


def get_active_calls(db: sqlite3.Connection | None = None) -> dict[str, CallState]:
    if db is None:
        return dict(_active_calls)
    return {state.call_id: state for state in list_active_calls(db)}


def get_active_call_count(db: sqlite3.Connection | None = None) -> int:
    if db is not None:
        return _count_persisted_active_calls(db)
    return len(_active_calls)


def reset_active_calls(db: sqlite3.Connection | None = None) -> None:
    if db is not None:
        _clear_persisted_active_calls(db)
    _active_calls.clear()
    _transfer_target_sids.clear()


def set_transfer_target_sid(call_id: str, target_sid: str) -> None:
    _transfer_target_sids[call_id] = target_sid


def clear_transfer_target_sid(call_id: str) -> str | None:
    return _transfer_target_sids.pop(call_id, None)


def update_active_call_started_at(
    call_id: str,
    started_at: int,
    db: sqlite3.Connection | None = None,
) -> None:
    if db is not None:
        _update_persisted_active_call_started_at(db, call_id, started_at)
    existing = _active_calls.get(call_id)
    if existing is not None:
        existing.started_at = started_at


def update_active_call_answered_at(
    call_id: str,
    answered_at: int,
    db: sqlite3.Connection | None = None,
) -> None:
    if db is not None:
        _touch_persisted_active_call(db, call_id)
    existing = _active_calls.get(call_id)
    if existing is not None:
        existing.answered_at = answered_at


def sweep_timed_out_calls(db: sqlite3.Connection | None = None) -> list[CallState]:
    timed_out = (
        sweep_timed_out_active_calls(db, OUTBOUND_TIMEOUT_MS)
        if db is not None
        else _sweep_timed_out_fallback_calls()
    )
    for state in timed_out:
        _active_calls.pop(state.call_id, None)
        logger.warn(
            "active-calls.timeout",
            callId=state.call_id,
            person=state.person_name,
            duration=max((now_ms() - state.started_at) // 1000, 0),
        )
    return timed_out


def _sweep_timed_out_fallback_calls() -> list[CallState]:
    current_ms = now_ms()
    timed_out: list[CallState] = []
    for call_id, state in list(_active_calls.items()):
        if (
            state.direction == "outbound"
            and state.answered_at is None
            and current_ms - state.started_at > OUTBOUND_TIMEOUT_MS
        ):
            timed_out.append(state)
            _active_calls.pop(call_id, None)
    return timed_out


# ── context assembly ──


def assemble_context(
    db: sqlite3.Connection,
    person: Person,
    audience: Audience,
    direction: Direction,
    active_calls: Mapping[str, CallState],
    tunnel_url: str | None = None,
    *,
    channel: Channel,
    modality: InteractionModality,
) -> str:
    variables = compute_variables(
        db,
        person,
        audience,
        direction,
        active_calls,
        tunnel_url,
        channel=channel,
        modality=modality,
    )
    return build_prompt(audience, variables)


async def assemble_outbound_context(
    db: sqlite3.Connection,
    person: Person,
    action: Action,
    audience: Audience,
    active_calls: Mapping[str, CallState],
    tunnel_url: str | None = None,
) -> str:
    base_prompt = assemble_context(
        db,
        person,
        audience,
        "outbound",
        active_calls,
        tunnel_url,
        channel="phone",
        modality="voice",
    )
    parts = [base_prompt, "", "## Outbound Call Context", f"You are calling about: {action.intent}"]
    if action.source_text:
        parts.append(f'Verbatim commitment: "{action.source_text}"')
    if action.context:
        parts.append(f"Additional context: {action.context}")

    try:
        has_transcript_history = db.execute(
            "SELECT 1 FROM transcript_chunks WHERE person_id = ? LIMIT 1",
            (person.id,),
        ).fetchone()
        if has_transcript_history is not None:
            search_query = f"{action.intent} {person.name or ''}".strip()
            excerpts = await hybrid_search(
                db,
                "transcripts",
                search_query,
                person_id=person.id,
                limit=3,
            )
            if excerpts:
                parts.append("")
                parts.append("Relevant conversation history:")
                parts.extend(f"> {excerpt.content[:200]}" for excerpt in excerpts)
    except Exception:
        pass

    recent_calls = get_recent_calls_by_person(db, person.id, 10)
    if recent_calls:
        tz = ZoneInfo(get_agent_config().hours.timezone)
        history_lines = [
            f"- {_format_call_date(call.started_at, tz)}: {call.summary}"
            for call in recent_calls
            if call.summary
        ]
        if history_lines:
            parts.append("")
            parts.append("Recent call history with this person:")
            parts.extend(history_lines)

    pending_actions = get_pending_actions_by_person(db, person.id)
    other_actions = [pending for pending in pending_actions if pending.id != action.id]
    if other_actions:
        parts.append("")
        parts.append("Other pending actions for this person:")
        parts.extend(f"- {pending.intent}" for pending in other_actions)

    return "\n".join(part for part in parts if part is not None)


def _format_call_date(started_at_ms: int, tz: ZoneInfo) -> str:
    started = datetime.fromtimestamp(started_at_ms / 1000, tz)
    return f"{started.strftime('%b')} {started.day}"


# ── end-of-call handling ──

ExtractionFn = Callable[[sqlite3.Connection, str, str, str], Awaitable[None]]
CallEndedCallback = Callable[[str, str, bool], None]

_extraction_pipeline: ExtractionFn | None = None
_call_ended_callback: CallEndedCallback | None = None
_pending_extraction_tasks: set[asyncio.Task[None]] = set()
_pending_bridge_tasks: set[asyncio.Task[None]] = set()


def set_extraction_pipeline(fn: ExtractionFn | None) -> None:
    global _extraction_pipeline
    _extraction_pipeline = fn
    if fn is None:
        cancel_pending_extraction_tasks()


def set_call_ended_callback(fn: CallEndedCallback | None) -> None:
    global _call_ended_callback
    _call_ended_callback = fn


async def drain_pending_extraction_tasks(timeout_ms: int) -> None:
    tasks = [task for task in _pending_extraction_tasks if not task.done()]
    if not tasks:
        return

    if timeout_ms > 0:
        _, pending = await asyncio.wait(tasks, timeout=timeout_ms / 1000)
    else:
        pending = set(tasks)

    if not pending:
        return

    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)


def cancel_pending_extraction_tasks() -> None:
    for task in list(_pending_extraction_tasks):
        if not task.done():
            task.cancel()


async def drain_pending_bridge_tasks(timeout_ms: int) -> None:
    tasks = [task for task in _pending_bridge_tasks if not task.done()]
    if not tasks:
        return

    if timeout_ms > 0:
        _, pending = await asyncio.wait(tasks, timeout=timeout_ms / 1000)
    else:
        pending = set(tasks)

    if not pending:
        return

    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)


def cancel_pending_bridge_tasks() -> None:
    for task in list(_pending_bridge_tasks):
        if not task.done():
            task.cancel()


async def handle_end_of_call_report_by_call_id(
    db: sqlite3.Connection,
    call_id: str,
    transcript: str,
    duration_seconds: int | None = None,
) -> None:
    call = get_call_by_id(db, call_id)
    if call is None:
        logger.warn("end-of-call.unknown", callId=call_id)
        return
    await _process_end_of_call(db, call, transcript, duration_seconds)


async def handle_end_of_call_report(
    db: sqlite3.Connection,
    external_id: str,
    transcript: str,
    duration_seconds: int | None = None,
) -> None:
    call = get_call_by_external_id(db, external_id)
    if call is None:
        logger.warn("end-of-call.unknown", externalId=external_id)
        return
    await _process_end_of_call(db, call, transcript, duration_seconds)


async def handle_completed_call_status(
    db: sqlite3.Connection,
    external_id: str,
    duration_seconds: int | None = None,
) -> None:
    call = get_call_by_external_id(db, external_id)
    if call is None:
        logger.warn("end-of-call.unknown", externalId=external_id)
        return

    target_sid = clear_transfer_target_sid(call.id)
    providers_config = get_providers_config()
    if target_sid is not None and providers_config.twilio is not None:
        try:
            await end_call(providers_config.twilio, target_sid)
        except Exception as exc:
            logger.warn(
                "call.transfer-target.cleanup.failed",
                callId=call.id,
                targetSid=target_sid,
                error=get_error_message(exc),
            )

    if call.ended_at is not None:
        remove_active_call(call.id, db)
        if duration_seconds is not None and call.duration is None:
            update_call_end(db, call.id, ended_at=call.ended_at, duration=duration_seconds)
        transcript = (call.transcript or "").strip()
        if _should_run_fallback_extraction(call, transcript):
            logger.info("call.completed.fallback-extraction", callId=call.id)
            _queue_extraction(db, call, transcript)
        return

    await _process_end_of_call(db, call, call.transcript or "", duration_seconds)


def handle_unanswered_outbound(db: sqlite3.Connection, external_id: str) -> None:
    call = get_call_by_external_id(db, external_id)
    if call is None:
        logger.warn("unanswered.unknown", externalId=external_id)
        return
    _mark_unanswered_call(db, call)


def handle_answered_outbound(db: sqlite3.Connection, external_id: str) -> None:
    call = get_call_by_external_id(db, external_id)
    if call is None:
        logger.warn("answered.unknown", externalId=external_id)
        return
    if call.answered_at is not None:
        logger.debug("call.answered.duplicate", callId=call.id)
        return

    answered_at = now_ms()
    update_call_answered_at(db, call.id, answered_at)
    update_active_call_answered_at(call.id, answered_at, db)
    emit_event("activity", {"type": "call_answered", "call_id": call.id})
    logger.info("call.answered", callId=call.id, actionId=call.action_id)


def handle_unanswered_outbound_by_call_id(db: sqlite3.Connection, call_id: str) -> None:
    call = get_call_by_id(db, call_id)
    if call is None:
        logger.warn("unanswered.unknown", callId=call_id)
        return
    _mark_unanswered_call(db, call)


async def _process_end_of_call(
    db: sqlite3.Connection,
    call: Call,
    transcript: str,
    duration_seconds: int | None = None,
) -> None:
    normalized_transcript = transcript.strip()
    if (
        call.ended_at is not None
        and (call.transcript or "").strip() == normalized_transcript
        and (duration_seconds is None or call.duration == duration_seconds)
    ):
        logger.debug("call.ended.duplicate", callId=call.id)
        return

    update_call_end(
        db,
        call.id,
        transcript=normalized_transcript,
        ended_at=now_ms(),
        duration=duration_seconds,
    )
    remove_active_call(call.id, db)
    if _call_ended_callback is not None:
        _call_ended_callback(call.id, call.external_id or call.id, True)
    person = get_person_by_id(db, call.person_id)
    descriptor = describe_call(call)
    emit_event("activity", {
        "type": "call_ended",
        "call_id": call.id,
        "direction": call.direction,
        "person_name": person.name if person else "Unknown",
        "duration_seconds": duration_seconds or 0,
        **interaction_event_payload(descriptor),
    })
    update_person_last_seen(db, call.person_id)
    logger.info(
        "call.ended",
        callId=call.id,
        duration=_format_duration(duration_seconds),
        audience=call.audience,
        direction=call.direction,
    )
    queued = _queue_extraction(db, call, normalized_transcript)
    if call.action_id and normalized_transcript:
        try:
            await check_satisfaction(db, call.id, call.person_id)
        except Exception as exc:
            logger.warn("call.satisfaction.failed", callId=call.id, error=get_error_message(exc))
    if call.action_id and not queued:
        finalize_in_progress_action(
            db,
            call.action_id,
            (
                "Call completed without fully resolving the action."
                if normalized_transcript
                else "Call completed without transcript to evaluate."
            ),
        )


def _mark_unanswered_call(db: sqlite3.Connection, call: Call) -> None:
    if call.ended_at is not None:
        logger.debug("call.unanswered.skip-ended", callId=call.id)
        return
    if call.answered_at is not None:
        logger.debug("call.unanswered.skip-answered", callId=call.id)
        return

    remove_active_call(call.id, db)
    update_call_end(db, call.id, ended_at=now_ms())
    if call.action_id:
        finalize_in_progress_action(db, call.action_id, "Call was not answered.")
    emit_event("activity", {"type": "call_unanswered", "call_id": call.id})
    logger.warn("call.unanswered", callId=call.id, actionId=call.action_id)
    if _call_ended_callback is not None:
        _call_ended_callback(call.id, call.external_id or call.id, False)


def _queue_extraction(
    db: sqlite3.Connection,
    call: Call,
    transcript: str,
) -> bool:
    if _extraction_pipeline is None or not transcript:
        return False
    pipeline = _extraction_pipeline

    async def _run_pipeline() -> None:
        await pipeline(db, call.id, call.person_id, transcript)

    mark_extraction_attempted(db, call.id)
    task = asyncio.create_task(_run_pipeline())
    _pending_extraction_tasks.add(task)
    task.add_done_callback(_pending_extraction_tasks.discard)
    task.add_done_callback(lambda finished: _handle_extraction_task_result(db, call, finished))
    return True


def _handle_extraction_task_result(
    db: sqlite3.Connection,
    call: Call,
    task: asyncio.Task[None],
) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("extraction.async.cancelled", callId=call.id)
    except Exception as exc:
        logger.error("extraction.async.error", callId=call.id, error=get_error_message(exc))
        if call.action_id:
            finalize_in_progress_action(
                db,
                call.action_id,
                "Call processing failed after the call ended.",
            )


def _should_run_fallback_extraction(call: Call, transcript: str) -> bool:
    if not transcript or _extraction_pipeline is None:
        return False
    if (
        call.ended_at is not None
        and call.last_extraction_attempt_at is not None
        and call.last_extraction_attempt_at >= call.ended_at
    ):
        return False
    return (
        call.facts_extracted == 0
        or call.commitments_extracted == 0
        or call.summary is None
    )


def _format_duration(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "unknown"
    minutes, seconds = divmod(duration_seconds, 60)
    return f"{minutes}m {seconds}s"


# ── bootstrap ──

LOCAL_OWNER_PHONE = "local"
LOCAL_ESCALATION_TIMEOUT = 20  # seconds
DEFAULT_VOICE_ID = "Hades"
MAX_BOOTSTRAP_ATTEMPTS = 3

VOICE_CATALOG = (
    {"label": "Male Voice (Hades)", "voice_id": "Hades"},
    {"label": "Male Voice (Mark)", "voice_id": "Mark"},
    {"label": "Male Voice (Clive)", "voice_id": "Clive"},
    {"label": "Female Voice (Olivia)", "voice_id": "Olivia"},
    {"label": "Female Voice (Pippa)", "voice_id": "Pippa"},
    {"label": "Female Voice (Orietta)", "voice_id": "Orietta"},
)

POCKET_VOICE_CATALOG = (
    {"label": "Male Voice (Hades)", "voice_id": "Hades", "clip": "hades.wav"},
    {"label": "Male Voice (Mark)", "voice_id": "Mark", "clip": "mark.wav"},
    {"label": "Male Voice (Clive)", "voice_id": "Clive", "clip": "clive.wav"},
    {"label": "Female Voice (Olivia)", "voice_id": "Olivia", "clip": "olivia.wav"},
    {"label": "Female Voice (Pippa)", "voice_id": "Pippa", "clip": "pippa.wav"},
    {"label": "Female Voice (Orietta)", "voice_id": "Orietta", "clip": "orietta.wav"},
)

_SEEDS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "seeds"


def get_default_voice_id(tts_config: TtsConfig) -> str:
    del tts_config
    return DEFAULT_VOICE_ID


def build_bootstrap_system_prompt() -> str:
    return (_SEEDS_DIR / "bootstrap.md").read_text(encoding="utf-8")


async def initiate_bootstrap_call(
    *,
    db: sqlite3.Connection,
    twilio_config: TwilioConfig,
    livekit_config: LiveKitConfig,
    customer_phone: str,
    person_id: str,
    action_id: str,
    voice_id: str,
    tunnel_url: str,
) -> dict[str, str] | None:
    call = None
    room_name: str | None = None
    call_sid: str | None = None
    try:
        call = insert_call(
            db,
            person_id=person_id,
            direction="outbound",
            channel="phone",
            modality="voice",
            audience="owner",
            action_id=action_id,
        )
        room_name = await create_room(
            livekit_config,
            call.id,
            {
                "callId": call.id,
                "personId": person_id,
                "audience": "owner",
                "direction": "outbound",
                "channel": "phone",
                "modality": "voice",
                "systemPrompt": build_bootstrap_system_prompt(),
                "voiceId": voice_id,
                "bootstrap": True,
            },
        )
        ws_url = build_authenticated_media_stream_url(tunnel_url, call.id, twilio_config.authToken)
        twiml = generate_stream_twiml(ws_url, {"callId": call.id})
        call_sid = await make_outbound_call(
            twilio_config,
            customer_phone,
            twiml,
            f"{tunnel_url}/webhook/twilio/status",
        )
        update_call_external_id(db, call.id, call_sid)
        start_action_attempt(db, action_id)
        add_active_call(
            CallState(
                call_id=call.id,
                person_id=person_id,
                person_name=None,
                audience="owner",
                direction="outbound",
                channel="phone",
                modality="voice",
                started_at=now_ms(),
            ),
            db,
        )
        logger.info("bootstrap.call.initiated", callId=call.id, callSid=call_sid)
        return {"call_id": call.id, "call_sid": call_sid}
    except Exception as exc:
        if call_sid is None and call is not None:
            if room_name is not None:
                try:
                    await delete_room(livekit_config, room_name)
                except Exception as cleanup_exc:
                    logger.warn(
                        "bootstrap.call.cleanup.room.failed",
                        room=room_name,
                        error=get_error_message(cleanup_exc),
                    )
            try:
                delete_call_by_id(db, call.id)
            except Exception as cleanup_exc:
                logger.warn(
                    "bootstrap.call.cleanup.call.failed",
                    callId=call.id,
                    error=get_error_message(cleanup_exc),
                )
        logger.error("bootstrap.call.failed", error=get_error_message(exc))
        return None


# ── incoming ──


class IncomingCallError(TypedDict):
    error: str
    status: int


class IncomingCallSuccess(TypedDict):
    twiml: str


async def handle_incoming_call(
    db: sqlite3.Connection,
    caller_phone: str | None,
    twilio_call_sid: str,
    tunnel_url: str,
) -> IncomingCallSuccess | IncomingCallError:
    if not caller_phone:
        return {"error": "No caller phone number", "status": 400}

    agent_config = get_agent_config()
    providers_config = get_providers_config()
    if providers_config.twilio is None:
        return {"error": "Twilio not configured", "status": 503}

    person = upsert_person(db, caller_phone)
    audience = "owner" if agent_config.owner.phone and caller_phone == agent_config.owner.phone else "public"
    system_prompt = assemble_context(
        db,
        person,
        audience,
        "inbound",
        get_active_calls(db),
        tunnel_url,
        channel="phone",
        modality="voice",
    )
    call = insert_call(
        db,
        person_id=person.id,
        direction="inbound",
        channel="phone",
        modality="voice",
        audience=audience,
        external_id=twilio_call_sid,
    )
    room_name = await create_room(
        providers_config.livekit,
        call.id,
        {
            "callId": call.id,
            "personId": person.id,
            "audience": audience,
            "direction": "inbound",
            "channel": "phone",
            "modality": "voice",
            "systemPrompt": system_prompt,
            "voiceId": agent_config.agent.voiceId or get_default_voice_id(providers_config.tts),
        },
    )
    add_active_call(
        CallState(
            call_id=call.id,
            person_id=person.id,
            person_name=person.name,
            audience=audience,
            direction="inbound",
            channel="phone",
            modality="voice",
            started_at=now_ms(),
        ),
        db,
    )
    descriptor = describe_interaction(direction="inbound", channel="phone", modality="voice")
    emit_event("activity", {
        "type": "call_started",
        "call_id": call.id,
        "direction": "inbound",
        "person_name": person.name,
        **interaction_event_payload(descriptor),
    })
    logger.info(
        "call.incoming",
        phone=caller_phone,
        person=person.name,
        audience=audience,
        callId=call.id,
        room=room_name,
    )
    ws_url = build_authenticated_media_stream_url(
        tunnel_url,
        call.id,
        providers_config.twilio.authToken,
    )
    return {"twiml": generate_stream_twiml(ws_url, {"callId": call.id})}


async def reconnect_call_to_stream(
    db: sqlite3.Connection,
    call_id: str,
    tunnel_url: str,
) -> str | None:
    """Rebuild stream TwiML for an existing call (reconnection after transfer failure, hold, etc.)."""
    call = get_call_by_id(db, call_id)
    if call is None:
        logger.warn("reconnect.skip", callId=call_id, reason="call not found")
        return None

    person = get_person_by_id(db, call.person_id) if call.person_id else None
    if person is None:
        logger.warn("reconnect.skip", callId=call_id, reason="person not found")
        return None

    providers_config = get_providers_config()
    if providers_config.twilio is None:
        logger.warn("reconnect.skip", callId=call_id, reason="twilio not configured")
        return None

    agent_config = get_agent_config()
    system_prompt = assemble_context(
        db,
        person,
        call.audience,
        call.direction,
        get_active_calls(db),
        tunnel_url,
        channel=call.channel,
        modality=call.modality,
    )
    await create_room(
        providers_config.livekit,
        call_id,
        {
            "callId": call_id,
            "personId": person.id,
            "audience": call.audience,
            "direction": call.direction,
            "channel": call.channel,
            "modality": call.modality,
            "systemPrompt": system_prompt,
            "voiceId": agent_config.agent.voiceId or get_default_voice_id(providers_config.tts),
        },
    )
    ws_url = build_authenticated_media_stream_url(
        tunnel_url, call_id, providers_config.twilio.authToken,
    )
    logger.info("reconnect.stream", callId=call_id, person=person.name)
    return generate_stream_twiml(ws_url, {"callId": call_id})


async def resume_call(
    db: sqlite3.Connection,
    call_id: str,
) -> bool:
    call = get_call_by_id(db, call_id)
    if call is None or not call.external_id:
        logger.warn("resume.skip", callId=call_id, reason="call not found or local-only")
        return False

    tunnel_url = get_tunnel_url()
    if not tunnel_url:
        logger.warn("resume.skip", callId=call_id, reason="tunnel unavailable")
        return False

    providers_config = get_providers_config()
    if providers_config.twilio is None:
        logger.warn("resume.skip", callId=call_id, reason="twilio not configured")
        return False

    stream_twiml = await reconnect_call_to_stream(db, call_id, tunnel_url)
    if stream_twiml is None:
        return False

    await update_live_call(providers_config.twilio, call.external_id, twiml=stream_twiml)
    logger.info("resume.ok", callId=call_id, externalId=call.external_id)
    return True


# ── outgoing ──


async def initiate_outbound_call(
    db: sqlite3.Connection,
    action: Action,
    tunnel_url: str,
) -> str | None:
    if not action.person_id:
        logger.warn("outbound.skip", actionId=action.id, reason="no person_id")
        return None

    person = get_person_by_id(db, action.person_id)
    if person is None:
        logger.warn("outbound.skip", actionId=action.id, reason="person not found")
        return None

    agent_config = get_agent_config()
    providers_config = get_providers_config()
    if providers_config.twilio is None:
        logger.warn("outbound.skip", actionId=action.id, reason="Twilio not configured")
        return None

    audience = "owner" if agent_config.owner.phone and person.phone == agent_config.owner.phone else "public"
    system_prompt = await assemble_outbound_context(
        db,
        person,
        action,
        audience,
        get_active_calls(db),
        tunnel_url,
    )

    call = None
    room_name: str | None = None
    call_sid: str | None = None
    try:
        call = insert_call(
            db,
            person_id=person.id,
            direction="outbound",
            channel="phone",
            modality="voice",
            audience=audience,
            action_id=action.id,
        )
        room_name = await create_room(
            providers_config.livekit,
            call.id,
            {
                "callId": call.id,
                "personId": person.id,
                "audience": audience,
                "direction": "outbound",
                "channel": "phone",
                "modality": "voice",
                "systemPrompt": system_prompt,
                "voiceId": agent_config.agent.voiceId or get_default_voice_id(providers_config.tts),
            },
        )
        ws_url = build_authenticated_media_stream_url(
            tunnel_url,
            call.id,
            providers_config.twilio.authToken,
        )
        twiml = generate_stream_twiml(ws_url, {"callId": call.id})
        call_sid = await make_outbound_call(
            providers_config.twilio,
            person.phone,
            twiml,
            f"{tunnel_url}/webhook/twilio/status",
        )
        update_call_external_id(db, call.id, call_sid)
        start_action_attempt(db, action.id)
        add_active_call(
            CallState(
                call_id=call.id,
                person_id=person.id,
                person_name=person.name,
                audience=audience,
                direction="outbound",
                channel="phone",
                modality="voice",
                started_at=now_ms(),
            ),
            db,
        )
        descriptor = describe_interaction(direction="outbound", channel="phone", modality="voice")
        emit_event("activity", {
            "type": "call_started",
            "call_id": call.id,
            "direction": "outbound",
            "person_name": person.name,
            **interaction_event_payload(descriptor),
        })
        logger.info(
            "call.outgoing",
            person=person.name,
            phone=person.phone,
            actionId=action.id,
            callSid=call_sid,
        )
        return call.id
    except Exception as exc:
        if call_sid is None and call is not None:
            if room_name is not None:
                try:
                    await delete_room(providers_config.livekit, room_name)
                except Exception as cleanup_exc:
                    logger.warn(
                        "outbound.cleanup.room.failed",
                        actionId=action.id,
                        room=room_name,
                        error=get_error_message(cleanup_exc),
                    )
            try:
                delete_call_by_id(db, call.id)
            except Exception as cleanup_exc:
                logger.warn(
                    "outbound.cleanup.call.failed",
                    actionId=action.id,
                    callId=call.id,
                    error=get_error_message(cleanup_exc),
                )
        logger.error("outbound.failed", actionId=action.id, error=get_error_message(exc))
        return None


async def initiate_local_escalation(
    db: sqlite3.Connection,
    action: Action,
) -> str | None:
    """Local escalation is no longer available — use the dashboard live page instead."""
    logger.info("escalation.local.unavailable", actionId=action.id)
    return None
