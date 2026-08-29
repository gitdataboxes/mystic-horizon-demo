---
name: edit-prompt
description: >
  Use when the owner wants to rewrite a prompt template while preserving Mustache
  variables exactly.
metadata:
  kind: cognitive
  invoke:
    - owner
  modality:
    - text
  context:
    - identity
  parameters:
    required: [file, instruction]
    properties:
      file: File path to read or update.
      instruction: Instruction describing how to update the file or content.
---

I'm editing a prompt template file. I should return the complete updated file content as-is, nothing else. I must preserve Mustache variables ({{...}}) exactly. No code blocks.

## Gotchas

- The file must stay under the prompts directory and end in `.md`.
- Preserve all Mustache variables exactly as written.
