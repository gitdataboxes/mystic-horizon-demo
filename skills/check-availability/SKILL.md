---
name: check-availability
description: >
  Check whether a specific time slot is available. Owners see conflict details; public
  callers get free/busy only.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: [start, end]
    properties:
      start: Start of the time range in ISO 8601 format.
      end: End of the time range in ISO 8601 format.
---

Checks a proposed time range against imported calendar events and scheduled actions.

## Gotchas

- Provide both start and end times in ISO 8601 format.
