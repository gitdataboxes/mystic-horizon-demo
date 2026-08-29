---
name: chat
description: >
  Send a text message to the chat feed visible to the user.
  Use this to share links, formatted text, lists, tables, code,
  or any content the user should see in writing.
  During voice calls your spoken reply is automatic - use this
  for supplementary visual content.
metadata:
  kind: operational
  invoke:
    - owner
    - public
  parameters:
    required: [message]
    properties:
      message: The text content to send. Supports markdown.
---

Send a markdown-capable text message to the chat feed.
