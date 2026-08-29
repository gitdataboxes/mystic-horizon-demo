---
name: manage-appointment
description: >
  Cancel or reschedule a scheduled action. Public callers can manage only their own
  appointments.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  modality:
    - voice
    - text
  parameters:
    required: [id, operation]
    properties:
      id: ID of the record or appointment to update.
      operation: Appointment operation: cancel or reschedule.
      start_at: Start time in ISO 8601 format.
      end_at: End time in ISO 8601 format.
---

Supports `cancel` and `reschedule` operations for scheduled appointments.

## Gotchas

- Use `cancel` or `reschedule` for operation.
- Rescheduling also requires both start_at and end_at.
