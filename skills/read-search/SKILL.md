---
name: read-search
description: >
  External web search via LLM (Perplexity/sonar). No identity injected — impersonal.
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

Delegates to the search model (Perplexity/sonar) with no soul/identity context.
