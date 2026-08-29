from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from tests.python_helpers import make_embedding

OWNER_PHONE = "+15551234567"
PUBLIC_PHONE = "+15550001111"
ALT_PHONE = "+15550002222"
TUNNEL_URL = "https://test-machine.tail1234.ts.net"
SAMPLE_TRANSCRIPT = (
    "Caller asked to move the meeting to Tuesday afternoon and send the quarterly report by Friday."
)

SkillResponse: TypeAlias = str | Exception | Callable[[str], str]


@dataclass(slots=True)
class IntegrationEnv:
    home: Path
    db: sqlite3.Connection
    tunnel_url: str = TUNNEL_URL


def sign_twilio_request(
    auth_token: str,
    url: str,
    params: Mapping[str, str],
) -> str:
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def make_cognitive_skill_runner(
    responses: Mapping[str, SkillResponse],
) -> Callable[..., Awaitable[str]]:
    async def runner(skill_name: str, *_args: object, **_kwargs: object) -> str:
        response = responses[skill_name]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(skill_name)
        return response

    return runner


async def make_transcript_embeddings(chunks: list[str]) -> list[list[float]]:
    return [make_embedding([float(index + 1)]) for index, _chunk in enumerate(chunks)]
