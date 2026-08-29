# prompts/ — Prompt Templates (Python)

## Purpose
Seed prompt templates used by the Python implementation. Mustache-format templates (`{{var}}`, `{{#section}}`) rendered at runtime with agent context.

## Ownership
- Shared

## Files
- `seeds/bootstrap.md` — Bootstrap identity discovery prompt (concise: speaking style first, then identity/soul phases, brief phone setup mention directing to Settings page). Medium-agnostic — no phone-call assumption (works from dashboard text chat too). Includes instruction not to narrate own state. No in-conversation credential collection — directs to Settings page.
- `seeds/bootstrap-v2.md` — Prior verbose version of the bootstrap prompt with same simplified connect phase (Settings page direction instead of in-conversation Twilio/Tailscale walkthrough)
- `seeds/owner-briefing.md` — Owner context briefing. Includes `{{#phoneSetupHint}}` section that nudges the owner agent to use setup skills (`read-setup`, `check-tailscale`, `write-twilio-credentials`, `read-twilio-numbers`, `write-twilio-number`, `activate-tunnel`) when Twilio is unconfigured.
- `seeds/public-workflow.md` — Public workflow instructions
- `seeds/shared-context.md` — Shared context for all prompts

## Key Patterns
- Templates use inline Mustache syntax and remain stable across the Python cutover
- Variables computed at runtime by `mystic/prompts.py`
- IDENTITY.md and SOUL.md injected into all prompts
- Audience-aware rendering: different templates for owner vs public callers
- `shared-context.md` injects `{{verbatimRecentContext}}` (unfinalized verbatim transcripts with timestamps and modality/channel tags) and `{{recentDaysSummary}}` (finalized day-level summaries from `day_summaries` table)
- `owner-briefing.md` no longer includes `{{recentCallsSummary}}` — context now comes from day summaries in shared-context
- `bootstrap.md` uses generic references ("your creator") — no `{{ownerName}}` variable; the agent discovers the owner's name during the bootstrap conversation itself
- `bootstrap.md` leads with speaking style (casual, short turns under ~25 words, one question at a time), then three phases: identity discovery (name, nature, vibe, emoji → `write` with `type: "identity"`), soul conversation (values, boundaries, max 2 follow-up questions → `write` with `type: "soul"`), and optional phone setup mention (directs to Settings page — no in-conversation credential collection). No explicit timebox — conciseness is enforced by the speaking style rules. Medium-agnostic — no phone-call assumption; explicitly instructs the agent not to narrate its own state ("just woke up").
- `bootstrap-v2.md` preserves the prior verbose version with same simplified connect phase (Settings page direction instead of in-conversation Tailscale/Twilio walkthrough)
- `owner-briefing.md` includes `{{#phoneSetupHint}}{{phoneSetupHint}}{{/phoneSetupHint}}` section — rendered when Twilio is unconfigured, instructs owner sessions to help directly with the Twilio/Tailscale setup skills instead of only sending the owner to Settings
