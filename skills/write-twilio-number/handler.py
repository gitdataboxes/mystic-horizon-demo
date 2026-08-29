"""Operational handler for write-twilio-number."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping, cast

from mystic.calls import (
    OwnedPhoneNumber,
    buy_phone_number,
    list_incoming_phone_numbers,
    search_available_numbers,
)
from mystic.config import (
    TwilioConfig,
    get_agent_config,
    get_error_message,
    get_home,
    get_tunnel_url,
    is_valid_e164,
    logger,
    set_tunnel_url,
    write_config,
)
from mystic.http import check_tailscale_ready, start_tunnel
from mystic.phone import ensure_phone_line_ready
from mystic.types import OperationalContext

_PLACEHOLDER_TUNNEL_URL = "https://placeholder.example.com"
_DIGITS_RE = re.compile(r"\D+")


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


def _twilio_webhook_urls(tunnel_url: str) -> tuple[str, str]:
    base_url = tunnel_url.rstrip("/")
    return f"{base_url}/webhook/twilio/voice", f"{base_url}/webhook/twilio/status"


def _twilio_payload(
    *,
    account_sid: str,
    auth_token: str,
    phone_number: str,
    phone_number_sid: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "accountSid": account_sid,
        "authToken": auth_token,
        "phoneNumber": phone_number,
    }
    if phone_number_sid:
        payload["phoneNumberSid"] = phone_number_sid
    return payload


def _phone_digits(value: str) -> str:
    return _DIGITS_RE.sub("", value)


def _canonical_e164(value: str) -> str:
    stripped = value.strip()
    if is_valid_e164(stripped):
        return stripped
    if stripped.startswith("+"):
        candidate = f"+{_phone_digits(stripped)}"
        if is_valid_e164(candidate):
            return candidate
    return stripped


def _find_owned_number(
    owned_numbers: list[OwnedPhoneNumber],
    requested_number: str,
) -> tuple[OwnedPhoneNumber | None, str | None]:
    normalized_number = requested_number.strip()
    if not normalized_number:
        return None, None

    canonical_number = _canonical_e164(normalized_number)
    for number in owned_numbers:
        if number["phoneNumber"] in {normalized_number, canonical_number}:
            return number, None

    requested_digits = _phone_digits(normalized_number)
    if not requested_digits:
        return None, None
    if len(requested_digits) < 4:
        return None, None

    matches = [
        number
        for number in owned_numbers
        if _phone_digits(number["phoneNumber"]).endswith(requested_digits)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        choices = ", ".join(number["phoneNumber"] for number in matches)
        return None, (
            f"More than one owned Twilio number matches {requested_number}: {choices}. "
            "Please provide the full number."
        )

    return None, None


async def _ensure_public_tunnel_url() -> tuple[str | None, str | None]:
    existing = get_tunnel_url()
    if existing and existing.startswith("https://"):
        return existing, None

    ready, reason = check_tailscale_ready()
    if not ready:
        return None, reason or "not ready"

    try:
        tunnel_url = await start_tunnel(get_agent_config().server.port)
    except Exception as exc:
        return None, get_error_message(exc)

    set_tunnel_url(tunnel_url)
    return tunnel_url, None


async def _reconcile_attached_number(twilio_config: TwilioConfig) -> tuple[str | None, str | None]:
    readiness = await ensure_phone_line_ready(
        port=get_agent_config().server.port,
        twilio_config=twilio_config,
        repair=True,
    )
    if readiness.status == "ok":
        return readiness.public_url, None
    return readiness.public_url, readiness.reason() or readiness.status


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    providers_path = get_home() / "config" / "providers.json"
    try:
        payload = _load_providers_payload(providers_path)
        account_sid, auth_token, saved_phone_number = _load_twilio_credentials(payload)
    except RuntimeError as exc:
        return str(exc)

    twilio_config = TwilioConfig(
        accountSid=account_sid,
        authToken=auth_token,
        phoneNumber=saved_phone_number or "+10000000000",
    )

    requested_number = params.get("phone_number")
    if isinstance(requested_number, str) and requested_number.strip():
        normalized_number = requested_number.strip()
        try:
            owned_numbers = await list_incoming_phone_numbers(twilio_config)
        except Exception as exc:
            return f"Owned-number lookup failed: {get_error_message(exc)}"
        owned_number, match_error = _find_owned_number(owned_numbers, normalized_number)
        if match_error is not None:
            return match_error

        if owned_number is not None:
            phone_number_sid = owned_number["sid"]
            payload["twilio"] = _twilio_payload(
                account_sid=account_sid,
                auth_token=auth_token,
                phone_number=owned_number["phoneNumber"],
                phone_number_sid=phone_number_sid,
            )
            payload.pop("twilioDraft", None)

            try:
                write_config("providers.json", payload)
            except Exception as exc:
                return f"Could not save Twilio number: {get_error_message(exc)}"

            logger.info("twilio.number.attached", number=owned_number["phoneNumber"])
            tunnel_url, tunnel_warning = await _ensure_public_tunnel_url()
            if tunnel_url is None:
                suffix = f" Tailscale tunnel was not activated: {tunnel_warning}." if tunnel_warning else ""
                return f"Attached and saved {owned_number['phoneNumber']}.{suffix}"

            attached_config = TwilioConfig(
                accountSid=account_sid,
                authToken=auth_token,
                phoneNumber=owned_number["phoneNumber"],
                phoneNumberSid=phone_number_sid,
            )
            public_url, reconcile_warning = await _reconcile_attached_number(attached_config)
            if reconcile_warning is not None:
                return (
                    f"Attached and saved {owned_number['phoneNumber']}. "
                    f"Tunnel active at {public_url or tunnel_url}, but phone readiness failed: {reconcile_warning}"
                )

            return (
                f"Attached and saved {owned_number['phoneNumber']}. "
                f"Tunnel active at {public_url or tunnel_url}; Twilio webhooks verified."
            )

        purchase_number = _canonical_e164(normalized_number)
        if not is_valid_e164(purchase_number):
            return (
                f"I couldn't find an owned Twilio number matching {normalized_number}. "
                "To buy a new number, pass the full E.164 number, for example +15551234567."
            )

        tunnel_url, tunnel_warning = await _ensure_public_tunnel_url()
        webhook_base_url = tunnel_url or _PLACEHOLDER_TUNNEL_URL
        voice_url, status_url = _twilio_webhook_urls(webhook_base_url)

        try:
            purchased = await buy_phone_number(
                twilio_config,
                purchase_number,
                voice_url,
                status_url,
            )
        except Exception as exc:
            return f"Purchase failed: {get_error_message(exc)}"

        phone_number_sid = None
        sid = purchased.get("sid")
        if isinstance(sid, str) and sid.strip():
            phone_number_sid = sid.strip()
        payload["twilio"] = _twilio_payload(
            account_sid=account_sid,
            auth_token=auth_token,
            phone_number=purchased["phoneNumber"],
            phone_number_sid=phone_number_sid,
        )
        payload.pop("twilioDraft", None)

        try:
            write_config("providers.json", payload)
        except Exception as exc:
            return f"Could not save Twilio number: {get_error_message(exc)}"

        logger.info("twilio.number.saved", number=purchased["phoneNumber"])
        if tunnel_url is None:
            suffix = f" Tailscale tunnel was not activated: {tunnel_warning}." if tunnel_warning else ""
            return f"Purchased and saved {purchased['phoneNumber']}.{suffix}"

        patched_config = TwilioConfig(
            accountSid=account_sid,
            authToken=auth_token,
            phoneNumber=purchased["phoneNumber"],
            phoneNumberSid=phone_number_sid,
        )
        public_url, reconcile_warning = await _reconcile_attached_number(patched_config)
        if reconcile_warning is not None:
            return (
                f"Purchased and saved {purchased['phoneNumber']}. "
                f"Tunnel active at {public_url or tunnel_url}, but phone readiness failed: {reconcile_warning}"
            )

        return (
            f"Purchased and saved {purchased['phoneNumber']}. "
            f"Tunnel active at {public_url or tunnel_url}; Twilio webhooks verified."
        )

    area_code = params.get("area_code")
    normalized_area_code = area_code.strip() if isinstance(area_code, str) and area_code.strip() else None

    try:
        numbers = await search_available_numbers(twilio_config, area_code=normalized_area_code)
    except Exception as exc:
        return f"Search failed: {get_error_message(exc)}"

    if not numbers:
        return "No numbers found. Try a different area code."

    lines = ["Available numbers:"]
    for number in numbers:
        lines.append(f"  {number['phoneNumber']} ({number['friendlyName']})")
    lines.append("")
    lines.append("Tell me which one you'd like and I'll buy it for you.")
    return "\n".join(lines)
