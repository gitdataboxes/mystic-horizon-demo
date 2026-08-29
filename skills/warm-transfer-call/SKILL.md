---
name: warm-transfer-call
description: >
  Warm-transfer the call: hold caller, announce to target, then bridge.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
  parameters:
    required: [destination]
    properties:
      destination: Transfer destination in E.164 format, or owner when supported.
      introduction: Brief intro message played to the warm-transfer target.
---

Announced transfer: holds the caller, calls the transfer target with a spoken introduction,
then bridges both into a Twilio conference. If the target doesn't answer, the caller
is reconnected to the agent. When the target hangs up, the caller is reconnected
to the agent automatically.
Requires Twilio config and an active Twilio call.

## Gotchas

- Only the owner can warm-transfer calls.
- Destination must be E.164 or `owner`.
