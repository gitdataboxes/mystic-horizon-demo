"""Operational handler for send-email."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from mystic.actions import send_email
from mystic.config import get_error_message, get_smtp_config
from mystic.types import OperationalContext


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    del db, ctx

    config = get_smtp_config()
    if config is None:
        return "SMTP not configured. Run init --connect-smtp first."

    to = params.get("to")
    if not isinstance(to, str) or not to.strip():
        return "Please provide a recipient email address."
    to = to.strip()
    if "@" not in to:
        return f"Invalid email address: {to}"

    subject = params.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        return "Please provide a subject line."

    body = params.get("body")
    if not isinstance(body, str) or not body.strip():
        return "Please provide an email body."

    try:
        await send_email(to, subject.strip(), body.strip())
    except Exception as exc:
        return f"Failed to send email: {get_error_message(exc)}"

    return f"Email sent to {to}."
