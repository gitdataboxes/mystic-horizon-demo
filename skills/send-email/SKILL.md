---
name: send-email
description: >
  Send an email via SMTP to a recipient address.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [to, subject, body]
    properties:
      to: Recipient email address.
      subject: Email subject line.
      body: Email body text to send.
---

Sends an email via the configured SMTP server. Requires `to` (recipient),
`subject`, and `body`. Uses the from-address from SMTP config.
