---
name: write-action
description: >
  Create a new action (commitment/follow-up). Parses due date from string.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: [intent]
    properties:
      intent: Action or appointment intent to create.
      due: Due date or time in ISO 8601 or another clear date format.
      start_at: Start time in ISO 8601 format.
      end_at: End time in ISO 8601 format.
---

Inserts an action with intent, optional due date, and server-derived source.

## Gotchas

- For scheduled appointments, provide both start_at and end_at.
