---
name: design-dashboard
description: >
  Use when the owner wants to create, redesign, or rewrite a dashboard page or stylesheet
  and return the complete updated file content.
metadata:
  kind: cognitive
  invoke:
    - owner
  modality:
    - text
  context:
    - identity
  parameters:
    required: [file]
    properties:
      file: File path to read or update.
      instructions: Instructions describing the dashboard changes to make.
      content: Main content to record, save, or send.
---

I'm editing a dashboard HTML or CSS file. I should return the complete updated file content as-is, with no code fences and no explanation.

## File naming

The `file` parameter must use the format `pages/<slug>.html` for dashboard pages — for example `pages/weather.html` or `pages/todo-list.html`. Only `.html` files are supported for pages. Use lowercase kebab-case slugs. Do not use `index` as a slug.

## Creating new pages

To create a new dashboard page, call `design-dashboard` with a new file path like `pages/my-page.html`. The file does not need to exist yet — it will be created automatically. New pages appear in the dashboard navigation immediately.

## Discovering existing pages

Before editing, call `read-dashboard` to list existing dashboard files and their structure. This lets you discover available pages, CSS, and naming conventions without asking the user for file paths.

## Gotchas

- Dashboard pages must use paths like `pages/<slug>.html`.
- Call `read-dashboard` first when you need to inspect existing files.
- If `content` is provided, save it directly instead of redesigning the file.
