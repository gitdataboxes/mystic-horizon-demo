---
name: judge-schedule
description: >
  Evaluate a batch of due actions and decide for each: act now (initiate call), wait (with
  a specific time), cancel, escalate to the owner, or send a desktop notification.
metadata:
  kind: cognitive
  invoke:
    - scheduler
  context:
    - identity
    - soul
  parameters:
    required: []
    properties:
  output-format: '[{ "id": "...", "decision": "act | wait | cancel | escalate | notify", "reason": "...", "wait_until": "ISO datetime (only if wait)" }]'
  json-mode: true
---

I'm deciding which pending actions to execute right now.

{{#recentCalls}}
Recent conversations with the people involved:
{{recentCalls}}
{{/recentCalls}}

For each action, I should decide:
- "act": initiate outbound call now
- "wait": delay (I must provide wait_until as ISO datetime)
- "cancel": intent is stale or moot
- "escalate": call the owner about this
- "notify": send a desktop notification (lightweight, no call)
