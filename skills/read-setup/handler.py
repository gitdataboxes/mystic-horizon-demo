"""Operational handler for read-setup."""

from __future__ import annotations

from typing import Mapping

from mystic.config import get_setup_status
from mystic.types import OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    _params: Mapping[str, object],
) -> str:
    status = get_setup_status()
    lines = [
        f"Identity: {'configured' if status.identity else 'not set'}",
        f"Soul: {'configured' if status.soul else 'not set'}",
    ]

    if status.tailscale_installed:
        lines.append("Tailscale: connected")
    else:
        lines.append(f"Tailscale: {status.tailscale_reason}")
        if status.tailscale_reason == "not installed":
            lines.append("  Install: curl -fsSL https://tailscale.com/install.sh | sh")
            lines.append("  Then: sudo tailscale up")
        elif status.tailscale_reason in {"daemon not running", "not authenticated"}:
            lines.append("  Run: sudo tailscale up")

    lines.append(f"Twilio: {'configured' if status.twilio else 'not configured'}")
    return "\n".join(lines)
