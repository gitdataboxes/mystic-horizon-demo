---
name: find-open-slots
description: >
  Find open windows in a time range by merging imported calendar events and scheduled
  actions.
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
      min_duration_minutes: Minimum slot length in minutes.
---

Returns free slots within a range so the agent can offer appointment choices directly.

## Gotchas

- Provide both start and end times. Set min_duration_minutes when needed.
