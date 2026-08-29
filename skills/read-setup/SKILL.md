---
name: read-setup
description: >
  Use when you need the current local setup and readiness status for the owner.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: []
    properties:
---

Returns what is configured and what still needs setup.
