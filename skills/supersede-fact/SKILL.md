---
name: supersede-fact
description: >
  Mark a fact as superseded (soft-delete). The fact is archived and excluded from active
  queries, search, and retrieval, but preserved in the database for history.
metadata:
  kind: operational
  invoke:
    - pipeline
  parameters:
    required: [id]
    properties:
      id: ID of the record or appointment to update.
---

Supersedes a fact by ID. The record stays in the database but is excluded from all active queries, FTS, and vector search.
