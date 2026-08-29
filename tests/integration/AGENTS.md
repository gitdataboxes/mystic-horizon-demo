# tests/integration/ — Integration Suites

## Purpose
Behavioral tests that exercise multi-module flows with the real in-memory database, seeded config files, and mocked external providers.

## Ownership
- Shared

## Conventions
- Prefer end-to-end assertions over implementation details: webhook handlers, call lifecycle, extraction, scheduler, and init flows should be validated through public module behavior.
- Use the shared helpers in `tests/integration/helpers.py` and fixtures from `conftest.py` instead of ad hoc provider stubs.
- Keep SQLite real. Integration tests should rely on schema initialization and forward-compatible column setup, including call extraction retry metadata.
- Retry/fallback behaviors should assert persisted state changes such as `answered_at`, `ended_at`, and `last_extraction_attempt_at`, not just log messages.
- Closed-loop tests should assert persisted action state across inbound extraction, scheduler judgment, outbound calling, and post-call requeue/finalization behavior.
- External Twilio and LiveKit APIs stay mocked at the module boundary; the DB, scheduler, and extraction pipeline should remain real inside the test.
- Calendar skill tests (`test_calendar_skills.py`) verify audience-scoped behavior: owner sees external calendar events + all scheduled actions, public callers see only their own appointments. Integration tests cover `read-calendar`, `check-availability`, `find-open-slots` read flows and `manage-appointment` write flows (reschedule + cancel via `execute_tool_calls`). Appointment booking uses `write-action` with `start_at`/`end_at` time slots directly (no `send_sms` mock on write-action — SMS orchestration is handled by the LLM via the separate `send-sms` skill). Hub calendar write-back tests verify `hub_sync_status` is set to `"pending"` atomically on write-action insert when hub config is present, hub create/update/delete are triggered from skill handlers, and retry/exhaustion behavior flows through the scheduler.
- Extraction retry tests (`test_extraction_retry.py`) validate the nightly extraction pipeline — `run_nightly_extraction` replaces per-call retries, creates day summaries, extracts facts and commitments at the day level, and backfills missing embeddings.
