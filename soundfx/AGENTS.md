# soundfx/ — Browser Sound Effects

## Purpose

Small OGG effects served by `mystic.web` for dashboard browser interactions.

## Ownership

- Dashboard frontend

## Conventions

- Keep files small and browser-friendly (`.ogg` today).
- Update the `/soundfx/{name}` allowlist in `mystic/web.py` whenever a new sound effect is added.
- `shell.js` currently uses `highendSwitchOn.ogg` and `highendSwitchOff.ogg` for UI switch feedback.
- Procedural Asteroids music and most game SFX live in `mystic/_assets/static/game.js`; this directory is for discrete reusable audio files.
