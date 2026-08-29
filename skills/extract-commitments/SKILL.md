---
name: extract-commitments
description: >
  Extract commitments — things promised, requested, or needing follow-up from a
  conversation transcript. Infers timing and urgency from context.
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
  output-format: '{ "commitments": [{ "content": "...", "intent": "...", "due": "ISO datetime string | null", "urgency": "normal | high" }] }'
  json-mode: true
---

I'm reviewing my conversation with {{personName}} for commitments — things that were promised, requested, or need follow-up.

I should infer appropriate timing from context:
- Explicit times → parse as due date
- "Urgent" or "ASAP" → null (immediate)
- Ambiguous → pick a reasonable default based on context
