---
name: read-calls
description: >
  Retrieve recent call history for a person. Public callers see only their own calls.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: []
    properties:
      person: Person name or search term to scope the lookup.
---

Returns recent calls with date, direction, duration, and summary.
