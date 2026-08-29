---
name: take-message
description: >
  Take a message for the owner when they're unavailable. Reads the message back for
  confirmation and notifies the owner.
metadata:
  kind: operational
  invoke:
    - public
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [content]
    properties:
      content: Main content to record, save, or send.
      name: Person or agent name.
      phone: Phone number in E.164 format.
      urgency: Urgency label such as low, normal, or high.
---

Record a caller message, read it back for confirmation, and notify the owner.
