---
name: write-faq
description: >
  Save a FAQ entry so the agent can answer the same question directly next time. Embeds
  for vector search. Agent-created entries survive FAQ file re-indexing.
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
      heading: Short question-style heading for the FAQ entry.
---

Upserts an FAQ chunk with embedding. Use `heading` for a short question summary.
