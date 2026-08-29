---
name: write-person
description: >
  Create or update a person record. Validates E.164 phone format.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [phone]
    properties:
      phone: Phone number in E.164 format.
      name: Person or agent name.
---

Upserts a person by phone number.
