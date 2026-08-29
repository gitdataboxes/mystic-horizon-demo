"""Operational handler for context7 — library doc lookup."""

from __future__ import annotations

import json
import os
from typing import Mapping
from urllib.parse import quote

from mystic.http import fetch_with_timeout
from mystic.types import OperationalContext


_API_BASE = "https://context7.com/api/v2"
_MAX_CHARS = 4000


def _api_key() -> str | None:
    key = os.environ.get("CONTEXT7_API_KEY")
    if key:
        return key
    try:
        from mystic.config import get_config_dir

        raw = json.loads((get_config_dir() / "providers.json").read_text())
        return raw.get("context7", {}).get("apiKey")
    except Exception:
        return None


async def execute(
    _db: object,
    _ctx: OperationalContext,
    params: Mapping[str, object],
) -> str:
    library = params.get("library")
    if not isinstance(library, str) or not library.strip():
        return "Please provide a library name."
    library = library.strip()
    topic = params.get("topic")
    topic_str = topic.strip() if isinstance(topic, str) and topic.strip() else library

    key = _api_key()
    headers: dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # 1. Resolve library ID
    search_url = (
        f"{_API_BASE}/libs/search"
        f"?libraryName={quote(library)}&query={quote(topic_str)}"
    )
    try:
        r = await fetch_with_timeout(search_url, headers=headers, timeout_label="context7-search")
    except Exception as exc:
        return f"Context7 search failed: {exc}"

    if r.status_code != 200:
        return f"Could not find library: {library}"

    results = r.json().get("results") or []
    if not results:
        return f"No results for: {library}"
    lib_id = results[0].get("id")
    if not lib_id:
        return f"No results for: {library}"

    # 2. Fetch docs (follow one redirect if library moved)
    ctx_url = (
        f"{_API_BASE}/context"
        f"?libraryId={quote(lib_id)}&query={quote(topic_str)}&type=txt"
    )
    try:
        r = await fetch_with_timeout(ctx_url, headers=headers, timeout_label="context7-docs")
    except Exception as exc:
        return f"Context7 doc fetch failed: {exc}"

    if r.status_code == 301:
        redirect_id = r.json().get("redirectUrl") or r.json().get("id")
        if redirect_id:
            ctx_url = (
                f"{_API_BASE}/context"
                f"?libraryId={quote(redirect_id)}&query={quote(topic_str)}&type=txt"
            )
            try:
                r = await fetch_with_timeout(ctx_url, headers=headers, timeout_label="context7-docs")
            except Exception as exc:
                return f"Context7 doc fetch failed: {exc}"

    if r.status_code == 202:
        return f"{library} is being indexed by Context7. Try again shortly."
    if r.status_code != 200:
        return f"Could not fetch docs for {library} (status {r.status_code})"

    text = r.text.strip()
    if not text:
        return f"No documentation found for {library} / {topic_str}"
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n\n[truncated]"
    return text
