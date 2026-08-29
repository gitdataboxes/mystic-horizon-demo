# mystic/_assets/ — Dashboard Assets

## Purpose

Source-owned dashboard templates, default dashboard pages, CSS, and browser JavaScript. These files define the persistent shell, setup/settings pages, HUD, chat sidebar, knowledge graph, and self-contained Asteroids game overlay.

## Ownership

- Shared frontend surface for `mystic.web`

## Conventions

- Keep the dashboard as the actual app surface, not a marketing page. The shell persists across htmx navigation; page fragments target `#page-content`.
- `shell.html` owns persistent chrome and overlay structure. `settings.html` and `setup.html` are server-rendered Mustache templates; keep data dependencies explicit in `web.py`.
- `shell.js` owns dashboard chrome, chat rendering, HUD, markdown rendering, stream/event de-duplication, audio switch sounds, and the `MysticShell` bridge registry. It primes dashboard history on load, replays chat `history` separately from live-call `hudHistory`, and keeps HUD scroll pinning user-respecting rather than always snapping to bottom.
- `voice.js` owns the dashboard LiveKit bridge. It should pass `provider_latency` events through to `MysticShell`, tag native transcription as `source: "stream"`, tag data-channel transcripts as `source: "event"`, merge overlapping `lk.transcription` chunks before rendering, and forward `hudHistory` separately from chat `history` when token responses include both.
- `game.js` owns its own LiveKit room, audio contexts, HUD state, procedural music/SFX, game mechanics, and localStorage-backed game volume controls. Dashboard voice is paused/resumed through `MysticShell`; game packets ride the game room, not the dashboard room.
- The HUD `LIVE :: PING` gauge prefers worker-published provider latency samples and falls back to turn timing when samples expire. Keep dashboard and game HUD formatting in sync.
- Tool cards may overlap. Track active tool cards as a collection keyed preferentially by tool name so completion events close the matching card instead of whichever card started last.
- HUD term boxes hide WebKit scrollbars until actively scrolled; preserve the `.is-scrolling` behavior when changing term overflow or scrollbar styling.
- Sound effects served from `/soundfx/{name}` are allowlisted in `web.py`; add new files there before referencing them from JavaScript.
- Keep static assets self-contained and browser-native. Avoid adding build steps unless the whole dashboard pipeline changes.
