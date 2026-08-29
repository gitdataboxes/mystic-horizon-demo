---
name: check-tailscale
description: >
  Use when you need to check whether Tailscale is installed, running, and authenticated.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: []
    properties:
---

Returns the current Tailscale readiness state and the next step to take.
