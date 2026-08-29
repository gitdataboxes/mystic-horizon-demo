"""Operational handler for write-fact."""

from __future__ import annotations

import sqlite3
from typing import Mapping, cast

from mystic.types import FactType, OperationalContext
from mystic.db import insert_fact
from mystic.memory import embed_chunks


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    content = params.get("content")
    if not isinstance(content, str) or not content:
        return "Please provide fact content."

    fact_type = params.get("factType")
    fact_type_name = fact_type if isinstance(fact_type, str) and fact_type else "context"

    embedding = None
    embeddings = await embed_chunks([content])
    if embeddings and embeddings[0]:
        embedding = embeddings[0]

    fact = insert_fact(
        db,
        person_id=ctx.person_id,
        call_id=ctx.call_id,
        type=cast(FactType, fact_type_name),
        content=content,
        confidence=0.8,
        source=ctx.source,
        embedding=embedding,
    )
    return f'Recorded fact: "{content}" ({fact_type_name}, id: {fact.id[:8]})'
