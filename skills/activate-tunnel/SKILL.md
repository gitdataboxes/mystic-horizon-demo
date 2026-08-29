---
name: activate-tunnel
description: >
  Use after Tailscale is ready and Twilio credentials + number are saved to start
  the tunnel and patch Twilio webhooks without restarting the daemon.
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

Starts the Tailscale Funnel tunnel and patches the Twilio phone webhook to point
at this agent's server. Requires Tailscale to be ready and Twilio fully configured
(credentials + phone number with SID).

## Gotchas

- Tailscale must be installed, running, and authenticated before calling this.
- Twilio credentials and an attached or purchased phone number must already be saved in providers.json.
- If the tunnel is already active, it will be reused and Twilio webhooks will be refreshed.
