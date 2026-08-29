---
name: read-facts
description: >
  Look up recorded facts about a person. Owner can query any person by name; public
  callers are scoped to their own facts.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: []
    properties:
      person: Person name or search term to scope the lookup.
---

Returns active (non-superseded) facts for a person, formatted with type and confidence.
