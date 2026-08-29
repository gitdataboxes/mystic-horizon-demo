"""Operational handler for check-tailscale."""

from __future__ import annotations

from typing import Mapping

from mystic.config import get_error_message
from mystic.http import (
    check_tailscale_ready,
    get_tailscale_funnel_status,
    get_tailscale_hostname,
)
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    _params: Mapping[str, object],
) -> str:
    try:
        ready, reason = check_tailscale_ready()
    except Exception as exc:
        return f"Could not check Tailscale status: {exc}"

    if ready:
        lines = ["Tailscale is ready."]
        try:
            lines.append(f"Hostname: https://{get_tailscale_hostname()}")
        except Exception as exc:
            lines.append(f"Hostname: unavailable ({get_error_message(exc)})")

        funnel_ready, funnel_status = get_tailscale_funnel_status()
        if funnel_ready:
            lines.append(f"Funnel status:\n{funnel_status}")
        else:
            lines.append(f"Funnel status: {funnel_status}")
        return "\n".join(lines)

    if reason == "not installed":
        return (
            "Tailscale is not installed.\n"
            "Install: curl -fsSL https://tailscale.com/install.sh | sh\n"
            "Then run: sudo tailscale up"
        )

    if reason == "daemon not running":
        return (
            "Tailscale is installed but the daemon is not running.\n"
            "Run: sudo tailscale up"
        )

    if reason == "not authenticated":
        return (
            "Tailscale daemon is running but not authenticated.\n"
            "Run: sudo tailscale up\n"
            "Then follow the browser link to log in."
        )

    return f"Tailscale is not ready: {reason}"
