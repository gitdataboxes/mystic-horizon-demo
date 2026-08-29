---
name: transfer-call
description: >
  Transfer the current phone call to another number.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
  parameters:
    required: [destination]
    properties:
      destination: Transfer destination in E.164 format, or owner when supported.
---

Cold-transfers the live call by replacing the active TwiML with a <Dial>.
Public callers may only transfer to "owner". Owner may transfer to any E.164
number or "owner". Requires Twilio config and an active Twilio call.

## Gotchas

- Public callers can only transfer to the owner.
