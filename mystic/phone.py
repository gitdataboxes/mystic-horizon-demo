"""Phone-line readiness reconciliation for Tailscale Funnel and Twilio."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from mystic.calls import get_incoming_phone_number, update_phone_webhook
from mystic.config import TwilioConfig, get_agent_config, get_error_message, get_providers_config, logger, set_tunnel_url
from mystic.http import (
    RequestTransport,
    check_tailscale_ready,
    get_tailscale_funnel_status,
    get_tailscale_hostname,
    start_tunnel,
    tailscale_funnel_matches_port,
)

ReadinessStatus = Literal["ok", "degraded", "offline", "not_configured"]


@dataclass(frozen=True, slots=True)
class CapabilityReadiness:
    status: ReadinessStatus
    reason: str = ""


@dataclass(slots=True)
class PhoneReadiness:
    status: ReadinessStatus
    public_url: str | None = None
    phone_number: str | None = None
    phone_number_sid: str | None = None
    tailscale: CapabilityReadiness = field(default_factory=lambda: CapabilityReadiness("not_configured"))
    funnel: CapabilityReadiness = field(default_factory=lambda: CapabilityReadiness("not_configured"))
    twilio: CapabilityReadiness = field(default_factory=lambda: CapabilityReadiness("not_configured"))
    webhook_voice_url: str | None = None
    webhook_status_url: str | None = None
    repaired: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def reason(self) -> str:
        if self.problems:
            return "; ".join(self.problems)
        for capability in (self.tailscale, self.funnel, self.twilio):
            if capability.reason:
                return capability.reason
        return ""


def expected_twilio_webhook_urls(public_url: str) -> tuple[str, str]:
    base_url = public_url.rstrip("/")
    return f"{base_url}/webhook/twilio/voice", f"{base_url}/webhook/twilio/status"


def _public_url(hostname: str) -> str:
    return f"https://{hostname.rstrip('.')}"


def _overall_status(statuses: Sequence[ReadinessStatus]) -> ReadinessStatus:
    if "offline" in statuses:
        return "offline"
    if "degraded" in statuses:
        return "degraded"
    if "ok" in statuses:
        return "ok"
    return "not_configured"


async def ensure_phone_line_ready(
    *,
    port: int | None = None,
    twilio_config: TwilioConfig | None = None,
    repair: bool = True,
    twilio_transport: RequestTransport | None = None,
) -> PhoneReadiness:
    """Inspect and optionally repair the public phone line.

    This is intentionally shared by startup, health, settings, and skills so
    those surfaces do not each invent their own definition of "phone ready".
    """
    providers = get_providers_config()
    config = twilio_config or providers.twilio
    port = port if port is not None else get_agent_config().server.port

    if config is None:
        return PhoneReadiness(status="not_configured", twilio=CapabilityReadiness("not_configured", "Twilio not configured"))

    readiness = PhoneReadiness(
        status="degraded",
        phone_number=config.phoneNumber,
        phone_number_sid=config.phoneNumberSid,
        twilio=CapabilityReadiness("degraded", "not checked"),
    )

    if not config.phoneNumberSid:
        readiness.twilio = CapabilityReadiness("offline", "Twilio phone number SID is missing")
        readiness.problems.append("Twilio phone number SID is missing")
        readiness.status = "offline"
        return readiness

    try:
        tailscale_ready, tailscale_reason = check_tailscale_ready()
    except Exception as exc:
        tailscale_ready = False
        tailscale_reason = get_error_message(exc)
    if not tailscale_ready:
        reason = tailscale_reason or "not ready"
        readiness.tailscale = CapabilityReadiness("offline", reason)
        readiness.funnel = CapabilityReadiness("offline", "Tailscale is not ready")
        readiness.problems.append(f"Tailscale not ready: {reason}")
        readiness.status = "offline"
        return readiness
    readiness.tailscale = CapabilityReadiness("ok")

    try:
        hostname = get_tailscale_hostname()
    except Exception as exc:
        reason = get_error_message(exc)
        readiness.tailscale = CapabilityReadiness("offline", reason)
        readiness.funnel = CapabilityReadiness("offline", "hostname unavailable")
        readiness.problems.append(f"Tailscale hostname unavailable: {reason}")
        readiness.status = "offline"
        return readiness

    public_url = _public_url(hostname)
    readiness.public_url = public_url

    try:
        funnel_ready, funnel_status = get_tailscale_funnel_status()
    except Exception as exc:
        funnel_ready = False
        funnel_status = get_error_message(exc)

    funnel_matches = funnel_ready and tailscale_funnel_matches_port(funnel_status, hostname, port)
    if funnel_matches:
        readiness.funnel = CapabilityReadiness("ok")
        set_tunnel_url(public_url)
    elif repair:
        try:
            public_url = await start_tunnel(port)
            readiness.public_url = public_url
            readiness.funnel = CapabilityReadiness("ok")
            readiness.repaired.append("tailscale_funnel")
            set_tunnel_url(public_url)
        except Exception as exc:
            reason = get_error_message(exc)
            readiness.funnel = CapabilityReadiness("offline", reason)
            readiness.problems.append(f"Tailscale Funnel not ready: {reason}")
            readiness.status = "offline"
            return readiness
    else:
        reason = "Funnel is not enabled for this app port"
        if funnel_status:
            reason = f"{reason}: {funnel_status}"
        readiness.funnel = CapabilityReadiness("degraded", reason)
        readiness.problems.append(reason)

    voice_url, status_url = expected_twilio_webhook_urls(readiness.public_url or public_url)
    readiness.webhook_voice_url = voice_url
    readiness.webhook_status_url = status_url

    try:
        number = await get_incoming_phone_number(config, config.phoneNumberSid, transport=twilio_transport)
    except Exception as exc:
        reason = get_error_message(exc)
        readiness.twilio = CapabilityReadiness("offline", reason)
        readiness.problems.append(f"Twilio number verification failed: {reason}")
        readiness.status = "offline"
        return readiness

    readiness.phone_number = number["phoneNumber"]
    readiness.phone_number_sid = number["sid"]
    number_mismatch = False
    if config.phoneNumber and number["phoneNumber"] != config.phoneNumber:
        number_mismatch = True
        readiness.problems.append(
            f"Configured Twilio number {config.phoneNumber} does not match Twilio inventory {number['phoneNumber']}"
        )

    current_voice_url = number["voiceUrl"].rstrip("/")
    current_status_url = number["statusCallback"].rstrip("/")
    desired_voice_url = voice_url.rstrip("/")
    desired_status_url = status_url.rstrip("/")
    if current_voice_url == desired_voice_url and current_status_url == desired_status_url:
        readiness.twilio = CapabilityReadiness(
            "degraded" if number_mismatch else "ok",
            "configured phone number mismatch" if number_mismatch else "",
        )
    elif repair:
        try:
            await update_phone_webhook(config, config.phoneNumberSid, voice_url, status_url, transport=twilio_transport)
            readiness.twilio = CapabilityReadiness(
                "degraded" if number_mismatch else "ok",
                "configured phone number mismatch" if number_mismatch else "",
            )
            readiness.repaired.append("twilio_webhooks")
        except Exception as exc:
            reason = get_error_message(exc)
            readiness.twilio = CapabilityReadiness("degraded", reason)
            readiness.problems.append(f"Twilio webhook patch failed: {reason}")
    else:
        reason = "Twilio webhooks do not match the current public URL"
        readiness.twilio = CapabilityReadiness("degraded", reason)
        readiness.problems.append(reason)

    readiness.status = _overall_status([readiness.tailscale.status, readiness.funnel.status, readiness.twilio.status])
    if readiness.status == "ok":
        logger.info("phone.ready", publicUrl=readiness.public_url, phoneNumber=readiness.phone_number)
    else:
        logger.warn("phone.not-ready", status=readiness.status, reason=readiness.reason())
    return readiness
