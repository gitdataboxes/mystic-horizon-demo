---
name: read-calendar
description: >
  Read upcoming calendar items. Owners see external calendar events plus scheduled
  actions. Public callers see only their own scheduled appointments.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: []
    properties:
      days: Number of days to look ahead. Defaults to 7.
      query: Search query or keyword filter.
---

Returns upcoming calendar items within the next few days, optionally filtered by keyword.
