---
name: edit-soul
description: >
  Rewrite SOUL.md based on an instruction. Receives the current soul as data (not as self-
  context) to avoid circular identity injection.
metadata:
  kind: cognitive
  invoke:
    - owner
  modality:
    - voice
    - text
  context:
    - identity
  parameters:
    required: [instruction]
    properties:
      instruction: Instruction describing how to update the file or content.
  soul-as-data: true
---

I'm editing my SOUL.md file. I should return the complete updated file content as-is, nothing else. No code blocks.

Current SOUL.md:

{{currentSoul}}
