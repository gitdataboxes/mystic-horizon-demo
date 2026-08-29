---
name: write-fact
description: >
  Record a new fact about a person. Embeds for vector search. Mid-call source derived
  server-side.
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
      factType: Fact type: identity, preference, relationship, or context.
---

Inserts a fact with embedding. Default confidence: 0.8.
