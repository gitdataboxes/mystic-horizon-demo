---
name: write-twilio-number
description: >
  Use when you need to attach an already-owned Twilio number, search available
  Twilio numbers, or buy the selected number.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - text
  parameters:
    required: []
    properties:
      area_code: Preferred area code when searching for available Twilio numbers.
      phone_number: Specific Twilio phone number to attach if already owned, otherwise buy.
---

Pass `area_code` to search available numbers. Pass `phone_number` to attach a
specific number if the account already owns it (full E.164 number or unique
trailing digits), otherwise buy that full E.164 number. When a number is
attached or purchased, it is saved to providers.json. If Tailscale is ready, the
tunnel is activated as needed and shared phone readiness verifies or repairs the
Twilio webhooks.

## Gotchas

- Use read-twilio-numbers first when the owner may already own a Twilio number.
- Search with area_code when the owner needs a new number, then buy one by
  passing phone_number.
- For owned numbers, a full E.164 number is safest; unique trailing digits are
  accepted when they match exactly one owned number.
- Twilio credentials must already be saved.
