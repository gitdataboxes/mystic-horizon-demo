---
name: write-soul
description: >
  Write SOUL.md content. Previous versions are saved to the journal.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [content]
    properties:
      content: Main content to record, save, or send.
---

Writes SOUL.md with automatic journal snapshots.
