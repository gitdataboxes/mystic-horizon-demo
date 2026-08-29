---
name: send-dtmf
description: >
  Send DTMF touch-tones on the current call.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
  parameters:
    required: [digits]
    properties:
      digits: DTMF digits to send. Use 0-9, *, #, A-D, or w for wait.
---

Generates DTMF audio tones and injects them into the call audio stream.
Used for navigating IVR menus on outbound calls.
Digits: 0-9, *, #, A-D, w.

## Gotchas

- Only works during an active voice call with audio available.
