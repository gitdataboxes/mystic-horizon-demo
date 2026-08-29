# Mystic-Horizon

Local CLI phone agent implemented in Python.

## Stack

- Runtime: Python 3.11+
- Server: `aiohttp`
- CLI: `click`
- DB: `sqlite3` + `sqlite-vec` + FTS5
- HTTP: `httpx`
- Logging: `structlog`
- Phone: Twilio + LiveKit
- STT: Moonshine
- TTS: Pocket TTS (local) or Inworld (cloud)
- Embeddings: local ONNX (`nomic-embed-text-v1.5`)
- LLM: OpenRouter or OpenAI-compatible endpoint

## Commands

- `bash scripts/bootstrap-python.sh` — create `.venv` and install dev deps
- `bash scripts/test-python.sh` — run pytest through `.venv`
- `bash scripts/typecheck.sh` — run pyright in `standard` mode through the repo venv
- `.venv/bin/python -m pytest` — run tests directly
- `.venv/bin/mystic-horizon --agent <name> <command>` — CLI entry point

## Conventions

- IDs: `uuid.uuid4()`
- Timestamps: Unix epoch ms via `int(time.time() * 1000)`
- Multi-agent: each agent has its own `APP_HOME` at `~/.mystic-horizon/{agent-name}/`
- Shared binaries live under `~/.mystic-horizon/bin/`
- `.venv/bin/python` is the default interpreter for repo-local work
- Shared data formats stay stable: SQLite schema, config JSON, SKILL.md, prompt templates, `IDENTITY.md`, `SOUL.md`

## Architecture

Flat modules (one `.py` file per domain, no sub-packages):

- `mystic/types.py` — all shared runtime, database, and skill types
- `mystic/config.py` — paths, config loading, identity, soul, logger, deps
- `mystic/http.py` — HTTP client helpers and Tailscale tunnel management
- `mystic/db.py` — schema, migrations, CRUD, active calls, timestamp utilities
- `mystic/llm.py` — LLM transport and JSON parsing
- `mystic/memory.py` — chunking, embeddings, retrieval, FAQ, extraction, retry
- `mystic/skills.py` — discovery, routing, self-context
- `mystic/prompts.py` — Mustache renderer, variable computation, prompt builder
- `mystic/actions.py` — lifecycle, scheduler, satisfaction
- `mystic/calls.py` — Twilio+TwiML client, call state, context, end-of-call, initiation
- `mystic/ink.py` — shared terminal rendering primitives, display-tool payload renderer
- `mystic/audio.py` — μ-law codec, resampler, DTMF generation, call recording
- `mystic/voice.py` — transcript collection, STT/TTS adapters, LiveKit pipeline, and voice tools
- `mystic/worker.py` — worker lifecycle, room metadata parsing, and agent entrypoint
- `mystic/livekit.py` — server lifecycle, room helpers, media stream bridge, browser audio bridge
- `mystic/web.py` — dashboard surface: auth, rendering, fragments, chat, SSE
- `mystic/server.py` — aiohttp app, webhooks, rate limit, media auth
- `mystic/runtime.py` — startup/shutdown orchestration
- `mystic/cli.py` — CLI entrypoint and all commands

## Testing

- Test framework: `pytest` + `pytest-asyncio`
- Static type checks: `bash scripts/typecheck.sh`
- Suite location: `tests/unit/` and `tests/integration/`
- DB tests use real in-memory SQLite
- HTTP/LLM integrations are mocked at the module boundary

## Status

The Python codebase is the canonical implementation. Legacy TypeScript sources and tests were removed during the all-Python cutover.
