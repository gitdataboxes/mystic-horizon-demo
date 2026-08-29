# tests/ — Test Infrastructure

## Purpose

Pytest suite for the Python implementation. Covers calls, extraction, scheduling, permissions, bootstrap, runtime orchestration, agent worker lifecycle, local escalation, bridge tasks, and infrastructure helpers.

## Files

- `conftest.py` — shared pytest fixtures
- `python_helpers.py` — temp `APP_HOME` helpers, sample configs, embedding helpers
- `integration/test_*.py` — end-to-end and cross-module flows
- `unit/test_*.py` — focused unit coverage
- `bench/test_*.py` — performance benchmarks via `pytest-benchmark` (marked `@pytest.mark.bench`, excluded from default test runs)

## Mocking Strategy

- LLM calls are patched with `unittest.mock.patch`
- Embeddings use deterministic helper vectors
- DB coverage uses real in-memory SQLite, not mocks
- Config coverage uses real files under temp `APP_HOME`
- Async behavior uses `pytest-asyncio` and `unittest.IsolatedAsyncioTestCase`

## Coverage Notes

- Integration coverage now includes the closed loop from inbound call to scheduler judgment to outbound follow-up.
- Unit coverage now includes `_agent_entrypoint` session-event wiring (transcript events published to `lk.agent.events` data channel via `publish_data`, unified text+voice pipeline via `generate_reply` with `interrupt()` before reply generation — no last-input modality tracking or `conversation_item_added` event publishing, agent speech delivered via native LiveKit transcription streaming, tool event forwarding via `on_tool_event` callback, stream deduplication, pipeline error `agent_error` events, room options fallback, chat-only rooms with text output options, heartbeat data channel packets resetting idle timer), turn handling config (`_build_turn_handling` with/without multilingual plugin, plugin init failure fallback), LiveKit binary resolution/install/version behavior, LiveKit server start with orphaned server auto-kill and explicit RTC TCP/UDP port flags, transcript entry parsing with multiline text, multi-agent port discovery with 3-port LiveKit ranges, `allocate_port` stride parameter, and interaction vocabulary (`test_interactions.py` — `describe_interaction`/`describe_call` for dashboard/phone/SMS/CLI channels, `format_interaction_brief` for human-readable labels).
- Unit coverage includes `RoomMetadata` parsing for `attentionCue`, `noResponseTimeout`, and `chatCallId` fields.
- Unit coverage includes runtime local-mode startup (no Twilio), phone readiness reconciliation startup, and bridge/phone task draining on shutdown.
- Config tests validate that `owner` section is optional, unknown keys are rejected, tunnel config round-trips correctly, `get_setup_status()` reports core completeness and Tailscale state, and `AgentRecordingConfig` parsing (disabled default, enabled round-trip, recordings dir).
- Preflight and runtime-service tests mock Tailscale (`check_tailscale_ready` in `mystic.http`) instead of cloudflared.
- Preflight tests use `_healthy_mocks()` baseline and mock `pocket_onnx_models_missing`, `embedding_model_missing`, `turn_detector_assets_missing`, and `is_python_package_available`. Preflight references `moonshine_voice` (not `moonshine_onnx`).
- After the module split, audio codec tests import from `mystic.audio`, HTTP/tunnel mocks target `mystic.http`, and worker tests import from `mystic.worker`.
- STT default model is `"small"` across all test fixtures (was `"base"`). Valid models: `"tiny"`, `"small"`, `"medium"`. `SttConfig` is a union of `MoonshineSttConfig | DeepgramSttConfig | UnconfiguredSttConfig`.
- Embedding tests mock `get_local_model_dir` since inference no longer lazy-downloads models.
- Skill handler tests cover `read-setup`, `write-twilio-credentials` (validation + draft save), `read-twilio-numbers`, `check-tailscale` hostname/Funnel reporting, `write-twilio-number` (search + owned-number attach by full number/trailing digits + purchase + draft-to-full promotion + phone readiness reconciliation), `chat` (message stripping and empty message rejection), and call-control skills (transfer, warm-transfer, hold, DTMF, SMS).
- Config tests cover `InworldTtsConfig` parsing and `TtsConfig` union serialization round-trip, `DeepgramSttConfig` parsing and `SttConfig` union serialization round-trip, `UnconfiguredSttConfig`/`UnconfiguredTtsConfig` parsing and round-trip.
- CLI tests cover Inworld TTS quick init, Inworld plugin install during `ensure_dependencies`, sibling key extraction for Inworld API keys, Deepgram STT init, Deepgram plugin install during `ensure_dependencies`, sibling key extraction for Deepgram API keys, Twilio draft key extraction, STT provider choice, `run_setup` dependency download with daemon handoff (no in-process runtime, no direct `_emit_json` call) and existing daemon reuse, `ensure_dependencies` `on_step` callback reporting and quiet mode for terminal output suppression, turn detector install and model download during init, `run_health` turn_detection and phone subsystem degraded reporting.
- Preflight tests verify unconfigured voice providers skip local model checks (no errors emitted).
- Dashboard web tests cover voice settings persistence, voice readiness for unconfigured providers, prepare endpoint background task creation with `setup_done` event coordination (no in-process runtime), SSE disconnect/shutdown handling (`_write_sse` plus stream wakeup), setup wizard form validation (missing cloud API keys → 422), setup form saves (local LLM + cloud voice configs, cloud LLM + local models, intelligence.json model propagation), sibling Twilio credential scanning, dashboard page redirect to setup when LLM unconfigured or STT/TTS not ready (voice readiness mocked for page rendering), htmx partial rendering (HX-Request header returns content-only without shell wrapper), split voice token/history `history` + `hudHistory`, setup page uses dedicated shell without sidebar, prepare step event broadcasting, and the relationship-only knowledge graph (`/dashboard/api/graph` returns `agent` hub + person nodes with strand edges and meta block; per-strand thread endpoint returns interaction history; agent node detail returns identity/soul/journal stats). All chat goes through LiveKit data channel — server-side chat endpoints removed. Setup state cleanup (`set_setup_db`, `set_setup_server`, `set_setup_runtime`) in test setup/teardown.
- Voice pipeline tests cover Inworld `resolve_effective_max_active_jobs` (not forced to 1 like Pocket), `PipelineConfig` with `DeepgramSttConfig`, `create_stt` Deepgram adapter, per-skill tool names in tool sets (public voice tools include `read-calendar`, `transfer-call`, `check-availability` with typed parameters, `chat`; owner phone voice tools include `read-soul`, `edit-soul`, `warm-transfer-call`; text-only skills like `design-dashboard` excluded from phone voice; owner dashboard tools include text setup skills like `read-twilio-numbers` because chat and mic share a room), `notify` tool in voice tool sets, raw-arguments invocation for per-skill tools with tool event emission, `chat` tool fast-path (bypasses skill handler dispatch, uses `on_send_text` callback directly), `build_agent_tools` tool count (6 for public voice with `display`/`notify`/`say`/`send_text`), `TranscriptCollector.add_tool_event` serializing tool events into the formatted transcript, and `MysticAgent.on_user_turn_completed` injecting a single timezone-aware `Current time:` system stamp before each turn.
- Action tests cover bootstrap action routing (Twilio via `initiate_bootstrap_call`, local-only rescheduling without judgment).
- All test call/skill fixtures include explicit `channel` and `modality` fields. `direction="chat"` replaced with `direction="inbound", channel="dashboard"|"cli", modality="text"` throughout.
- Runtime tests verify `start_full` only (no separate `start_bootstrap` mode); `Runtime` has no `mode` field. Runtime startup uses `start_nightly_loop`/`drain_nightly_loop` (replaces `start_retry_loop`/`drain_retry_loop`) and starts `_start_phone_reconcile_loop` only when Twilio is configured. `start_runtime_from_setup` tests verify progress hooks, `before_server` callback invocation, and that setup DB is kept open on start failure.
- Call-control tests cover cold transfer, warm transfer, hold/resume, DTMF tone generation, and dial-action webhook handling. Transfer tests mock `update_live_call`; dial-action tests mock `reconnect_call_to_stream`.
- Call recording tests verify `CallRecorder` stereo WAV output, PCM16LE conversion, stop idempotency, and `start_call_recorder` config gating.
- Calendar tests cover ICS expansion, timezone formatting, sync upsert/stale deletion, sync interval, availability/open-slot computation, and reminder deduplication. Hub write-back tests cover `create_hub_event`/`update_hub_event`/`delete_hub_event` with mocked HTTP, 401 retry, `maybe_retry_hub_sync` dispatch, attempt counting/exhaustion, `_build_vevent` RFC 5545, and guard clauses. Calendar plumbing tests cover schema v6 migration (drift detection for phase 1, phase 2, and day summaries), hub sync CRUD, scheduled action CRUD, external event CRUD, day summary CRUD, prompt schedule variable computation (including `verbatim_recent_context` and `recent_days_summary`), scheduler calendar sync/reminder/hub-retry integration, CLI `--connect-calendar` and `--connect-hub-calendar` wizards, and write-action time-slot + hub sync support. Integration tests verify audience-scoped calendar reads and appointment management (reschedule, cancel) with hub write-back assertions. Nightly extraction tests verify `run_nightly_extraction` creates day summaries, extracts facts/commitments at the day level, and rebuilds person summaries.
- LiveKit room tests cover `require_assignment` flag on `dispatch_agent_to_room`, room creation helpers, token generation, agent dispatch, and `parse_transcript_entries` replaying `Tool [event]` lines as structured dicts (alongside speaker entries) so the dashboard can reconstruct tool activity from persisted history.
- Schema tests now target v9: fresh schema applies 001–009, legacy upgrade paths from v1 remove `is_game_mode` from `calls` (drop-column migration) while preserving the `game_scores` leaderboard table added in 008, and `create_migration` emits `010_*.sql` for the next user-defined migration. `get_recent_calls` gained a thin SQL-formatting refactor and is now exported from `mystic.db`.
- Game-room tests (in `test_voice_pipeline.py` / `test_web.py`): `parse_room_metadata` defaults `kind` to `"dashboard"`, parses `kind="game"`, and rejects unknown kinds back to `"dashboard"`. `POST /dashboard/api/game/token` is auth-gated, creates an ephemeral `game-{slug}-{hex}` room via `_create_dashboard_room_with_agent_dispatch` with `metadata["kind"] == "game"`, returns `token`/`url`/`roomName`/`participantName` without a `callId`, and produces a unique room per request. `GET /dashboard/api/game/scores` reads the leaderboard written by `insert_game_score`.
- LLM tests verify the `read-search` task is hardcoded to OpenRouter `perplexity/sonar-pro` even when a custom local backend is configured — the search path uses a dedicated OpenRouter backend with the OpenRouter referrer/title headers and is not subject to custom-backend header suppression.
- Agent entrypoint tests cover dashboard text-first owner sessions keeping audio I/O wired (so MIC toggle can enable voice), voice-control start re-enabling session audio, and dashboard tool events being persisted into both the call transcript and the chat transcript (`[mm:ss] Tool [event]: {json}`) for refresh-replay.
- Dashboard config tests cover `DashboardConfig` token round-trip, `ensure_dashboard_token` persistence, `write_dashboard_file` history on overwrite, `list_dashboard_files` exclusion of `.history/`, dashboard path derivation, and daemon socket path.
- Dashboard route tests (`test_server_auth.py`) verify route registration includes `/dashboard/*` and `/static/*` paths (including `/dashboard/api/voice/token`, `/dashboard/api/voice/disconnect`), `/dashboard/page/home` redirects to login without session, `/dashboard/login` accepts token and sets session cookie, authenticated status fragment renders, and voice token endpoint returns room credentials.
- CLI surface tests (`test_cli_resources.py`) cover `setup` JSON emission, `chat --message` JSON output, and `health` exit code propagation.
- Skill tests verify the 48-skill registry count plus direct per-skill routing (tools now named directly e.g. `write-action`, `read-soul` — no `TOOL_SKILL_MAP`), `build_skill_tool_schema` with parameters and gotchas, `build_tools_for_context` audience/modality filtering (owner text includes `design-dashboard` and phone setup tools; public voice includes `transfer-call` but excludes `design-dashboard`), modality metadata parsing (`take-message` has `voice`+`text`, `design-dashboard` has `text` only), and `SkillParameters` parsing with required/properties.
- LLM tests cover `stream_llm_with_tools` with async `on_text` callback (verifies awaitable callbacks are properly awaited).
- Source tests verify `derive_source` with direct skill names (e.g., `write-fact`, `write-action`, `read-facts`) instead of the old `(tool_name, type_name)` pattern.
- `TEST_AGENT_CONFIG` no longer includes `owner.name`; `owner.phone` is the only owner field.
- All test imports use flat module paths (`from mystic.db import ...`, not `from mystic.db.actions import ...`).

## Benchmarks

- Suite: `tests/bench/` — audio codec, text chunking, DB operations (vec/FTS), embedding inference, prompt rendering, memory retrieval, TTS synthesis, and LLM TTFT
- Runner: `bash scripts/bench.sh` or `.venv/bin/python -m pytest tests/bench -m bench --benchmark-only`
- Benchmarks use `@pytest.mark.bench` and are excluded from default `test-python.sh` runs
- Embedding benchmarks auto-skip if the model is not downloaded locally
- `conftest.py` provides `bench_db` (empty schema) and `populated_db` (200 chunks across 5 people, 4 calls each) fixtures

## Commands

- `bash scripts/test-python.sh` — unit + integration (excludes benchmarks)
- `bash scripts/bench.sh` — performance benchmarks only
- `bash scripts/typecheck.sh` — pyright static type checks
- `.venv/bin/python -m pytest`
