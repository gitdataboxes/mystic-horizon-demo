"""Operational handler for read-twilio-numbers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, cast

from mystic.calls import list_incoming_phone_numbers
from mystic.config import (
    TwilioConfig,
    get_error_message,
    get_home,
)
from mystic.types import OperationalContext


def _load_providers_payload(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read providers.json: {get_error_message(exc)}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("providers.json must contain a JSON object.")
    return cast(dict[str, object], raw)


def _load_twilio_credentials(payload: Mapping[str, object]) -> tuple[str, str, str | None]:
    twilio_raw = payload.get("twilio")
    if isinstance(twilio_raw, Mapping):
        account_sid = twilio_raw.get("accountSid")
        auth_token = twilio_raw.get("authToken")
        phone_number = twilio_raw.get("phoneNumber")
        if isinstance(account_sid, str) and account_sid.strip() and isinstance(auth_token, str) and auth_token.strip():
            normalized_phone = phone_number.strip() if isinstance(phone_number, str) and phone_number.strip() else None
            return account_sid.strip(), auth_token.strip(), normalized_phone

    draft_raw = payload.get("twilioDraft")
    if isinstance(draft_raw, Mapping):
        account_sid = draft_raw.get("accountSid")
        auth_token = draft_raw.get("authToken")
        if isinstance(account_sid, str) and account_sid.strip() and isinstance(auth_token, str) and auth_token.strip():
            return account_sid.strip(), auth_token.strip(), None

    raise RuntimeError("Twilio credentials not configured. Use write with type twilio-credentials first.")


async def execute(
    _db: object,
    _ctx: OperationalContext,
    _params: Mapping[str, object],
) -> str:
    providers_path = get_home() / "config" / "providers.json"
    try:
        payload = _load_providers_payload(providers_path)
        account_sid, auth_token, attached_number = _load_twilio_credentials(payload)
    except RuntimeError as exc:
        return str(exc)

    twilio_config = TwilioConfig(
        accountSid=account_sid,
        authToken=auth_token,
        phoneNumber=attached_number or "+10000000000",
    )

    try:
        numbers = await list_incoming_phone_numbers(twilio_config)
    except Exception as exc:
        return f"Lookup failed: {get_error_message(exc)}"

    if not numbers:
        return "No phone numbers are provisioned on this Twilio account yet."

    lines = ["Numbers on this Twilio account:"]
    for number in numbers:
        marker = "  (attached to this agent)" if attached_number and number["phoneNumber"] == attached_number else ""
        lines.append(f"  {number['phoneNumber']} ({number['friendlyName']}){marker}")
    if attached_number is None:
        lines.append("")
        lines.append("Tell me which one you'd like to attach and I'll wire it up.")
    return "\n".join(lines)
