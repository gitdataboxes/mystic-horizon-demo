---
name: check-satisfaction
description: >
  After a call ends, check whether any pending actions for this person were addressed.
  Returns satisfied/partial/not_satisfied for each action.
metadata:
  kind: cognitive
  invoke:
    - pipeline
  context:
    - identity
    - soul
    - person
    - actions
    - call-origin
  parameters:
    required: []
    properties:
  output-format: '[{ "id": "...", "status": "satisfied | partial | not_satisfied", "confidence": 0.0-1.0, "reason": "..." }]'
  json-mode: true
---

A call just ended with {{personName}}.

{{#pendingActions}}
I have pending actions for this person:
{{pendingActions}}

For each action, I need to decide: was its intent addressed by this call?
{{/pendingActions}}
