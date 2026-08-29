---
name: recall-self
description: >
  Read scrapbook journal snapshots of SOUL.md or IDENTITY.md. Owner only.
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
      file_type: Journal file type to inspect: soul or identity.
      timestamp: Journal snapshot timestamp in epoch milliseconds.
---

Lists recent self-journal entries by default, or reads one full snapshot when a timestamp is provided.

## Gotchas

- Omit timestamp to list snapshots. Provide timestamp to read one entry.
