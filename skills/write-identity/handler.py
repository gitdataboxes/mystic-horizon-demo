"""Operational handler for write-identity."""

from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

from mystic.config import get_agent_config, write_config, write_identity, logger
from mystic.types import Identity, OperationalContext


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    name = params.get("name")
    creature = params.get("creature")
    vibe = params.get("vibe")
    emoji = params.get("emoji")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(creature, str)
        or not creature.strip()
        or not isinstance(vibe, str)
        or not vibe.strip()
        or not isinstance(emoji, str)
        or not emoji.strip()
    ):
        return "All fields (name, creature, vibe, emoji) are required."

    identity = Identity(
        name=name.strip(),
        creature=creature.strip(),
        vibe=vibe.strip(),
        emoji=emoji.strip(),
    )
    write_identity(identity, trigger="write-identity")

    try:
        agent_config = asdict(get_agent_config())
        agent_config["agent"]["name"] = identity.name
        write_config("agent.json", agent_config)
        logger.info("write.identity.agent-name-synced", name=identity.name)
    except Exception as exc:
        logger.warn("write.identity.agent-name-sync-failed", error=str(exc))

    return f"Identity written! You are {identity.name}, a {identity.creature}. Vibe: {identity.vibe} {identity.emoji}"
