---
name: read-transcripts
description: >
  Search conversation transcripts using hybrid vector + full-text search. Returns matching
  excerpts scoped by caller permissions.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: [query]
    properties:
      query: Search query or keyword filter.
---

Searches indexed transcript chunks using hybrid retrieval (vector + FTS5).
Public callers are scoped to their own conversations.
Owner can search across all transcripts.
Returns top 5 matches with content previews.
