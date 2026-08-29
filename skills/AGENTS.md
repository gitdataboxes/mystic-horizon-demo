# skills/ — Skill Definitions (Python)

## Purpose
48 skill directories shared by the Python implementation. Each skill has a `SKILL.md` (metadata + prompt template) and optional `handler.py` (Python implementation for operational skills).

## Ownership
- Shared

## Structure

Each skill directory contains:
- `SKILL.md` — Frontmatter metadata (`kind`, `invoke`, `required_context`, `has_handler`) + Mustache prompt template
- `handler.py` — Python handler (operational skills and cognitive skills with custom pre/post-processing)

## Skills

### Cognitive (9) — LLM-powered, SKILL.md prompt only (unless noted)
| Skill | Purpose | Notes |
|-------|---------|-------|
| `check-satisfaction` | 3-state satisfaction judgment | |
| `extract-commitments` | Extract action commitments from call | |
| `extract-facts` | Extract facts from transcript | |
| `judge-schedule` | Scheduler decision (act/wait/cancel/escalate/notify) | |
| `summarize-call` | Summarize call transcript | |
| `summarize-person` | Build person summary from facts | |
| `edit-soul` | LLM revise SOUL.md | Has handler.py |
| `edit-prompt` | LLM revise prompt template | Has handler.py |
| `design-dashboard` | LLM revise dashboard HTML/CSS files | Has handler.py |

### Operational (39) — DB/file ops, SKILL.md + handler.py
| Skill | Purpose |
|-------|---------|
| `chat` | Send a text message to the chat feed (markdown-capable, no modality restriction) |
| `read-actions` | List due/pending/completed actions |
| `read-calls` | List recent calls with transcripts |
| `read-facts` | Search person facts |
| `read-faq` | Search FAQ by keyword |
| `read-people` | List known people |
| `read-search` | Hybrid search across knowledge base |
| `read-dashboard` | Read dashboard HTML/CSS files |
| `recall-self` | Read journal snapshots of SOUL.md or IDENTITY.md |
| `read-setup` | Check agent setup status (identity, soul, Tailscale, Twilio) |
| `read-soul` | Read SOUL.md content |
| `read-transcripts` | Search transcript chunks |
| `read-twilio-numbers` | List Twilio IncomingPhoneNumbers already owned by the configured account |
| `write-action` | Create new action (supports optional `start_at`/`end_at` time slots for scheduled events; pushes to hub calendar when configured) |
| `write-fact` | Create new fact |
| `write-faq` | Save FAQ entry for vector search retrieval |
| `write-person` | Create/update person info |
| `write-identity` | Write IDENTITY.md |
| `write-soul` | Write SOUL.md |
| `write-twilio-credentials` | Validate and save Twilio Account SID + Auth Token |
| `write-twilio-number` | Search available Twilio numbers or attach/buy one; promotes Twilio draft credentials to full config and reconciles phone readiness when a tunnel is available |
| `take-message` | Take a message for the owner, create action, and send desktop notification |
| `supersede-fact` | Soft-delete a fact (archived, excluded from queries) |
| `send-sms` | Send SMS message via Twilio |
| `send-email` | Send email via configured SMTP server |
| `send-dtmf` | Send DTMF touch-tones on the current call |
| `hold-call` | Put the current phone call on hold |
| `transfer-call` | Cold transfer the current call to another number |
| `warm-transfer-call` | Warm transfer: hold caller, announce to target, then bridge |
| `check-availability` | Check whether a time slot is available (owners see conflicts, public gets free/busy) |
| `find-open-slots` | Find open windows in a time range by merging calendar events and scheduled actions |
| `manage-appointment` | Cancel or reschedule a scheduled action (public can manage own only, sends SMS confirmation, syncs changes to hub calendar) |
| `read-calendar` | Read upcoming calendar items (owners see all, public sees own appointments) |
| `edit-action` | Update action status/due date |
| `edit-person` | Modify person name/info |
| `edit-config` | Update agent configuration |
| `activate-tunnel` | Activate/reuse Tailscale Funnel and verify Twilio phone webhooks through shared phone readiness |
| `check-tailscale` | Check Tailscale installation/authentication plus hostname and Funnel status |
| `context7` | Fetch library documentation via Context7 API |

## Key Patterns
- SKILL.md format is stable across the cutover — same frontmatter and Mustache templates as existing agent data
- `invoke` field controls permissions: `owner`, `public`, `pipeline`, `scheduler`
- Handlers loaded dynamically via `importlib` in the Python skill router
- Skills return natural language strings — the agent LLM reads them aloud to callers
- Twilio setup is split intentionally: `write-twilio-credentials` can save credentials as `providers.json["twilioDraft"]` before a phone number exists; `read-twilio-numbers` is read-only inventory; `write-twilio-number` attaches an owned number by full number or unique trailing digits, or purchases a full E.164 number, then refreshes Twilio webhooks through `mystic.phone.ensure_phone_line_ready()` when a public tunnel is available.
- `activate-tunnel` should delegate phone-line repair to `ensure_phone_line_ready()` rather than calling low-level Tailscale/Twilio helpers directly. The low-level `mystic.http` helpers only answer Tailscale state or start/stop Funnel.
