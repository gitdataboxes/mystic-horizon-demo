---
name: read-dashboard
description: >
  Use when you need to list or read editable dashboard files for the owner.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: []
    properties:
      file: File path to read or update.
      query: Search query or keyword filter.
---

Reads dashboard files from the agent's editable dashboard surface.

## Gotchas

- Omit file to list available dashboard files.
