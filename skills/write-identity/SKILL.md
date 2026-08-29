---
name: write-identity
description: >
  Write IDENTITY.md with name, creature, vibe, emoji. Syncs agent.json name field.
metadata:
  kind: operational
  invoke:
    - owner
  modality:
    - voice
    - text
  parameters:
    required: [name, creature, vibe, emoji]
    properties:
      name: Person or agent name.
      creature: Creature or archetype that describes the agent.
      vibe: Short phrase describing the agent's vibe.
      emoji: Emoji that represents the agent's identity.
---

Writes IDENTITY.md and syncs the agent name in agent.json.
