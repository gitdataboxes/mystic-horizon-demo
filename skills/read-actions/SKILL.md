---
name: read-actions
description: >
  List actions by status. Owner sees all actions; public callers see only their own.
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
      status: Status value to filter by or apply.
---

Returns actions filtered by status (default: pending).
Owner gets all pending actions; public gets their own.
