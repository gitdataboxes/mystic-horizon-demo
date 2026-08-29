---
name: edit-action
description: >
  Update an action's status or due date.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [id]
    properties:
      id: ID of the record or appointment to update.
      status: Status value to filter by or apply.
      due: Due date or time in ISO 8601 or another clear date format.
      result: Result text to store when updating an action status.
---

Modifies an existing action's status or reschedules it.
