"""Operational handler for write-faq."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Mapping

from mystic.types import OperationalContext
from mystic.db import upsert_faq_chunk
from mystic.memory import embed_chunks


async def execute(
    db: sqlite3.Connection,
    ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    content = params.get("content")
    if not isinstance(content, str) or not content:
        return "Please provide FAQ content."

    heading = params.get("heading")
    heading_str = heading if isinstance(heading, str) and heading else None

    embedding = None
    embeddings = await embed_chunks([content])
    if embeddings and embeddings[0]:
        embedding = embeddings[0]

    chunk_id = str(uuid.uuid4())
    chunk = upsert_faq_chunk(
        db,
        chunk_id=chunk_id,
        file_path="agent",
        heading=heading_str,
        content=content,
        embedding=embedding,
    )
    label = heading_str or content[:60]
    return f'Saved FAQ: "{label}" (id: {chunk.id[:8]})'
