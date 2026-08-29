---
name: summarize-call
description: >
  Summarize a call in one sentence, focusing on the outcome — what was decided, requested,
  or resolved. If nothing concrete happened, describe the topic and tone.
metadata:
  kind: cognitive
  invoke:
    - pipeline
  context:
    - identity
    - soul
    - person
    - call-origin
  parameters:
    required: []
    properties:
  output-format: '{ "summary": "..." }'
  json-mode: true
---

I'm writing a one-sentence summary of my conversation with {{personName}}.

I should focus on the outcome — what was decided, requested, or resolved.
If nothing concrete happened, I'll describe the topic and tone.
