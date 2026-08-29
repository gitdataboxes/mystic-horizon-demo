---
name: context7
description: >
  Look up current library or framework documentation via Context7.
metadata:
  kind: operational
  invoke:
    - owner
    - pipeline
  modality:
    - voice
    - text
  parameters:
    required: [library]
    properties:
      library: "Package or framework name (e.g. aiohttp, react, livekit-agents)."
      topic: "Specific function, class, or concept to look up."
---

Fetches up-to-date documentation excerpts from Context7.

## Gotchas

- Use topic to narrow results. Without it you get general overview docs.
- First lookup for an unindexed library may return a retry-later message.
