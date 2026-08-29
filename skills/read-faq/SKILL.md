---
name: read-faq
description: >
  Search FAQ entries using hybrid vector + full-text search. Not person-scoped.
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

Searches indexed FAQ chunks using hybrid retrieval.
Returns top 3 matches.
