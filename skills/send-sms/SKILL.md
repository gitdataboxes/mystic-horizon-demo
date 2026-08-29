---
name: send-sms
description: >
  Send an SMS message via Twilio to a phone number.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [message]
    properties:
      message: SMS message body to send.
      phone: Phone number in E.164 format.
---

Sends an SMS via Twilio. Phone defaults to the current person's contact number
if not specified. Message is the SMS body text.
