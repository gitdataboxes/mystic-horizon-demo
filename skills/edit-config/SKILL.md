---
name: edit-config
description: >
  Use when the owner wants to update an allowlisted config value safely.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: [file, path, value]
    properties:
      file: File path to read or update.
      path: Dotted config path to update.
      value: New config value. Can be a string, number, boolean, list, or object.
---

Validates against config allowlist, then updates the nested config field.

## Gotchas

- Only allowlisted config fields can be modified.
- `value` may be a structured JSON value, not just a string.
