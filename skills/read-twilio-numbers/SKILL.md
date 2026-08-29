---
name: read-twilio-numbers
description: >
  Use during owner phone setup when the owner asks which Twilio phone numbers
  they already own, or after Twilio credentials are saved to help them choose
  an existing number to attach to this agent.
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

Lists phone numbers already provisioned on the configured Twilio account
(IncomingPhoneNumbers). Read-only — does not buy, attach, or modify webhooks.

## Gotchas

- Twilio credentials must already be saved via write-twilio-credentials.
- To attach one of these numbers to this agent, follow up with write-twilio-number
  passing the chosen phone_number.
