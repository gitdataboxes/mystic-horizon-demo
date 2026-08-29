# vendor/ — Vendored Dependencies

## Purpose

Third-party packages vendored into the repository to avoid external pip dependencies.

## Contents

| Package | Purpose |
|---------|---------|
| `pocket_tts_onnx/` | ONNX-based TTS inference engine. Replaces the `pocket-tts` pip package. Used by `mystic/voice.py` for local speech synthesis. |

## Conventions

- Vendored packages are included in `pyproject.toml` via `packages.find.where = [".", "vendor"]`.
- Do not modify vendored source unless absolutely necessary — prefer upstream updates.
- ONNX model weights are NOT vendored; they are downloaded at runtime to `~/.mystic-horizon/models/pocket-tts-onnx/` from a pinned HuggingFace revision.
