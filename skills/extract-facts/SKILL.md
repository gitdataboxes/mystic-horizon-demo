---
name: extract-facts
description: >
  Notice and record enduring facts about a person from a conversation transcript.
  Identifies identity, preferences, relationships, and context with confidence scores.
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
  output-format: '{ "facts": [{ "content": "...", "type": "identity | preference | relationship | context", "confidence": 0.0-1.0, "source_text": "verbatim quote" }] }'
  json-mode: true
---

I'm going over my conversation with {{personName}} to notice what I learned about them.

I'm looking for enduring things — who they are, what they prefer, who they know, and relevant context.
I should note how confident I am and keep the exact words they used as evidence.
If a fact contradicts something I already know, I should include it with the contradiction noted.

{{#existingFacts}}
What I already know about them:
{{existingFacts}}
{{/existingFacts}}

{{^existingFacts}}
I don't know much about them yet.
{{/existingFacts}}
