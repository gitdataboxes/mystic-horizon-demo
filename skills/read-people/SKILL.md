---
name: read-people
description: >
  Search for people by name or phone. Owner only.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [query]
    properties:
      query: Search query or keyword filter.
---

Fuzzy-searches the people table by name or phone number.
