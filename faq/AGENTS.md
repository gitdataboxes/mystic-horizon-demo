# faq/ — Seed FAQ Content

## Purpose

Markdown FAQ seed files copied into each agent's `APP_HOME/faq/` during init/setup and indexed for retrieval.

## Ownership

- Shared owner-facing knowledge seeds

## Conventions

- Keep entries operational and skill-oriented: name the skill the agent should use when the answer describes a setup workflow.
- Phone setup should reflect the current split flow: verify Tailscale, save Twilio credentials, use `read-twilio-numbers` before attaching an owned number with `write-twilio-number`, search/buy only when the owner needs a new number, then use `activate-tunnel` to verify the public line.
- Do not include real credentials, phone numbers, or environment-specific URLs in seed FAQ files.
