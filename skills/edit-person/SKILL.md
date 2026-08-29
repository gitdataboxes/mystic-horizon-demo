---
name: edit-person
description: >
  Update a person's name by phone number.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [phone, name]
    properties:
      phone: Phone number in E.164 format.
      name: Person or agent name.
---

Updates the name for an existing person record.
