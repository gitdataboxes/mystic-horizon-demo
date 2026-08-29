---
name: summarize-person
description: >
  Rebuild a one-line summary of who a person is, based on all known facts and call
  history. Focuses on identity, relationship to the owner, and current relevance.
metadata:
  kind: cognitive
  invoke:
    - pipeline
  context:
    - identity
    - soul
    - person
    - recent-calls
  parameters:
    required: []
    properties:
  output-format: '{ "summary": "..." }'
  json-mode: true
---

I'm writing a one-line summary of {{personName}} based on everything I know.

I should focus on who they are, their relationship to me, and anything currently relevant.

{{#recentCalls}}
Recent conversations with them:
{{recentCalls}}
{{/recentCalls}}
