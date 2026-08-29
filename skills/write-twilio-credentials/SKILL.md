---
name: write-twilio-credentials
description: >
  Use when you need to validate and save Twilio credentials.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: [account_sid, auth_token]
    properties:
      account_sid: Twilio Account SID to validate and save.
      auth_token: Twilio Auth Token paired with the account SID.
---

Validates the credentials against the Twilio API, then saves them to providers.json.

## Gotchas

- The credentials are validated against Twilio before saving.
