"""Operational handler for write-twilio-credentials."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Mapping, cast

from mystic.config import get_error_message, get_home, logger, write_config
from mystic.http import fetch_with_timeout
from mystic.types import OperationalContext

_TWILIO_API = "https://api.twilio.com/2010-04-01"


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


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    account_sid = params.get("account_sid")
    auth_token = params.get("auth_token")
    if not isinstance(account_sid, str) or not account_sid.strip():
        return "account_sid is required."
    if not isinstance(auth_token, str) or not auth_token.strip():
        return "auth_token is required."

    normalized_sid = account_sid.strip()
    normalized_token = auth_token.strip()
    credentials = base64.b64encode(f"{normalized_sid}:{normalized_token}".encode("utf-8")).decode("utf-8")

    try:
        response = await fetch_with_timeout(
            f"{_TWILIO_API}/Accounts/{normalized_sid}/IncomingPhoneNumbers.json?PageSize=1",
            headers={"Authorization": f"Basic {credentials}"},
            timeout_ms=10_000,
            timeout_label="twilio.validate",
        )
    except Exception as exc:
        return f"Could not reach Twilio API: {get_error_message(exc)}"

    if response.status_code == 401:
        return "Invalid credentials. Check your Account SID and Auth Token on the Twilio console."
    if response.status_code < 200 or response.status_code >= 300:
        return f"Twilio API returned {response.status_code}. Check your credentials."

    providers_path = get_home() / "config" / "providers.json"
    try:
        payload = _load_providers_payload(providers_path)
    except RuntimeError as exc:
        return str(exc)

    twilio_raw = payload.get("twilio")
    if isinstance(twilio_raw, Mapping) and isinstance(twilio_raw.get("phoneNumber"), str) and twilio_raw.get("phoneNumber"):
        twilio_payload = dict(cast(Mapping[str, object], twilio_raw))
        twilio_payload["accountSid"] = normalized_sid
        twilio_payload["authToken"] = normalized_token
        payload["twilio"] = twilio_payload
        payload.pop("twilioDraft", None)
    else:
        payload.pop("twilio", None)
        payload["twilioDraft"] = {
            "accountSid": normalized_sid,
            "authToken": normalized_token,
        }

    try:
        write_config("providers.json", payload)
    except Exception as exc:
        return f"Could not save Twilio credentials: {get_error_message(exc)}"

    logger.info("twilio.credentials.written")
    return "Twilio credentials validated and saved."
