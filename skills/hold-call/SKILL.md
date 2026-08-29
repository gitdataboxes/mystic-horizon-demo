---
name: hold-call
description: >
  Put the current phone call on hold.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
  parameters:
    required: []
    properties:
      hold_message: Message the caller hears while the call is on hold.
---

Replaces the active call audio with a hold message loop.
After about 30 seconds, the caller is automatically reconnected to the agent.
Requires Twilio config and an active Twilio call.

## Gotchas

- Requires an active Twilio-backed live call.
