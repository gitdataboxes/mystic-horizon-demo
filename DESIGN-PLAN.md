# Design Refactor Implementation Plan

Phased execution of the DESIGN.md manifesto. Each phase is independently
shippable. No phase depends on a framework change, build tool, or new
dependency beyond a font file.

---

## Phase 0: Font & Variables

**Goal:** Replace the design foundation without changing any visual layout.
After this phase, the old colors/spacing/radii are gone from the stylesheet
and every value references the new system.

### 0.1 Add display font

Add Share Tech Mono (or Space Mono) as a web font. Options:

- **Google Fonts link** in `shell.html` and `setup-shell.html` `<head>`:
  `<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">`
- **Self-hosted** `.woff2` in `mystic/_assets/static/` served via the
  existing static asset route (preferred for local-first).

Define font stacks in CSS variables:

```css
:root {
  --font-display: "Share Tech Mono", "Space Mono", ui-monospace, monospace;
  --font-body: "IBM Plex Mono", "JetBrains Mono", "SF Mono", Consolas, monospace;
}
```

**Files:** `style.css` `:root` block, `shell.html`, `setup-shell.html`

### 0.2 Replace color variables

Replace the current 11-variable palette with the monochrome system:

```
OLD                          NEW
--bg: #0d1014            ->  --void: hsl(165, 12%, 5%)
--panel: #131922         ->  --surface: hsl(165, 15%, 10%)
--panel-2: #18202b       ->  (removed, use --surface)
--line: #283240          ->  --dim: hsl(165, 20%, 24%)
--text: #edf3fb          ->  --glow: hsl(165, 55%, 65%)
--muted: #9fb0c4         ->  --mid: hsl(165, 35%, 42%)
--accent: #7fd0ff        ->  (removed, use --glow)
--accent-2: #ffd36e      ->  (removed)
--danger: #ff8b8b        ->  --warn: hsl(30, 40%, 50%)
--success: #8de0a7       ->  (removed, use --glow)
--shadow: 0 24px 60px... ->  (removed, no box-shadows)
```

Add speaker hue variables and the opacity scale isn't stored as variables --
it's applied directly.

**Approach:** Find-and-replace every `var(--old)` reference in `style.css`.
This is a single-file change. Every occurrence of `var(--accent)` becomes
`var(--glow)`, every `var(--line)` becomes `var(--dim)`, etc.

**Files:** `style.css`

### 0.3 Replace spacing values

Normalize all padding/margin/gap values to the 8px grid:

```
OLD              NEW
0.25rem      ->  0.25rem  (--space-xs, keep)
0.3rem       ->  0.25rem
0.35rem      ->  0.5rem
0.4rem       ->  0.5rem
0.45rem      ->  0.5rem
0.55rem      ->  0.5rem
0.6rem       ->  0.5rem
0.65rem      ->  0.5rem
0.7rem       ->  0.5rem
0.75rem      ->  0.5rem
0.85rem      ->  1rem
0.9rem       ->  1rem
0.95rem      ->  1rem
1.2rem       ->  1rem
1.25rem      ->  1.5rem
1.5rem       ->  1.5rem   (keep)
2rem         ->  2rem     (keep)
3rem         ->  3rem     (keep)
```

Define spacing variables in `:root` and use them. This is tedious but
mechanical -- go rule by rule through the stylesheet.

**Files:** `style.css`

### 0.4 Replace border-radius values

```
OLD                    NEW
border-radius: 0.85rem  ->  border-radius: 0
border-radius: 0.9rem   ->  border-radius: 2px
border-radius: 0.95rem  ->  border-radius: 0
border-radius: 1rem     ->  border-radius: 0
border-radius: 1.2rem   ->  border-radius: 0
border-radius: 1.5rem   ->  border-radius: 0
border-radius: 999px    ->  border-radius: 0 (mode toggle: keep 999px)
border-radius: 0.4rem   ->  border-radius: 2px (inline code)
border-radius: 0.45rem  ->  border-radius: 0 (tool cards)
border-radius: 0.5rem   ->  border-radius: 2px (waveform, terminal)
border-radius: 0.6rem   ->  border-radius: 0
```

Keep `border-radius: 999px` only on the mode toggle pill in setup, where the
rounded shape is part of the radio-button affordance. Input fields get 2px.
Everything else goes to 0.

**Files:** `style.css`

### 0.5 Replace type sizes

Swap hardcoded font sizes to CSS variables:

```
OLD                    NEW
font-size: 0.68rem  ->  font-size: var(--text-xxs)
font-size: 0.72rem  ->  font-size: var(--text-xs)
font-size: 0.75rem  ->  font-size: var(--text-xs)
font-size: 0.78rem  ->  font-size: var(--text-sm)
font-size: 0.82rem  ->  font-size: var(--text-sm)
font-size: 0.85rem  ->  font-size: var(--text-sm)
font-size: 0.92rem  ->  font-size: var(--text-base)
font-size: 1.05rem  ->  font-size: var(--text-base)
clamp(...)          ->  font-size: var(--text-xl)
```

**Files:** `style.css`

### Phase 0 validation

After all 0.x steps, the app should look noticeably different (monochrome,
sharp corners, different spacing) but be functionally identical. Test:

- [ ] Login page renders, token input works
- [ ] Setup form loads, mode toggles work, preparation runs
- [ ] Live page: chat messages send/receive, voice call connects
- [ ] Settings page: forms render, save works
- [ ] Home/Calls/People/Actions pages: HTMX fragments load
- [ ] Mobile (<920px): sidebar stacks, content readable

---

## Phase 1: Structural Cleanup

**Goal:** Remove visual chrome (gradients, backdrop-filter, box-shadow,
decorative backgrounds) and flatten the surface hierarchy.

### 1.1 Remove body background gradients

```css
/* BEFORE */
body {
  background:
    radial-gradient(circle at top left, rgba(127, 208, 255, 0.08), transparent 28rem),
    radial-gradient(circle at bottom right, rgba(255, 211, 110, 0.06), transparent 22rem),
    var(--bg);
}

/* AFTER */
body {
  background: var(--void);
}
```

**Files:** `style.css`

### 1.2 Remove backdrop-filter and translucent backgrounds

Sidebar and topbar currently use `backdrop-filter: blur(18px)` and
`rgba(8, 11, 16, 0.78)` backgrounds. Replace with:

```css
.sidebar {
  background: var(--void);
  /* remove backdrop-filter */
}

.topbar {
  background: var(--void);
  /* remove backdrop-filter */
}
```

**Files:** `style.css`

### 1.3 Flatten panel backgrounds

```css
/* BEFORE */
.panel {
  background: linear-gradient(180deg, rgba(24, 32, 43, 0.9), rgba(19, 25, 34, 0.95));
  box-shadow: var(--shadow);
}

/* AFTER */
.panel {
  background: transparent;
  border: 1px solid var(--dim);
}
```

**Files:** `style.css`

### 1.4 Flatten message bubbles

```css
/* BEFORE */
.msg {
  border: 1px solid rgba(127, 208, 255, 0.12);
  background: rgba(8, 11, 16, 0.52);
}

/* AFTER */
.msg-agent {
  border: none;
  background: transparent;
}

.msg-user {
  border: 1px solid var(--dim);
  background: hsla(140, 15%, 10%, 0.3);
}
```

**Files:** `style.css`

### 1.5 Flatten nav links and buttons

Remove hover lift (`transform: translateY(-1px)`). Replace with border-color
brightening only:

```css
.nav-link {
  border: none;
  border-left: 2px solid transparent;
  background: transparent;
  /* remove border-radius */
}

.nav-link:hover {
  color: var(--glow);
  /* remove transform */
}

button:hover {
  border-color: var(--glow);
  /* remove transform */
}
```

**Files:** `style.css`

### Phase 1 validation

- [ ] No gradients visible anywhere
- [ ] No blurred backgrounds
- [ ] No floating/lifted elements on hover
- [ ] Panels defined by borders only
- [ ] Agent messages appear borderless
- [ ] All pages still functionally correct

---

## Phase 2: Typography Hierarchy

**Goal:** Apply the display/body font split and establish the visual hierarchy
between structural text and content text.

### 2.1 Apply display font to structural elements

Add `font-family: var(--font-display)` to:

- `.brand-kicker`, `.eyebrow` (already uppercase)
- `.brand-name`, `.page-header h1` (add uppercase + letter-spacing)
- `.panel h2`, `.panel h3` (add uppercase + letter-spacing)
- `.nav-link` (add uppercase + letter-spacing)
- `button` (add uppercase + letter-spacing)
- `.agent-card-name`
- `.voice-panel-label`
- `th` (table headers)
- `label > span` (form field labels)
- `.msg-meta` (speaker labels)

### 2.2 Update speaker labels

In `live.js`, change the meta text:

```js
// BEFORE
meta.textContent = role === "user" ? "You" : role === "agent" ? "Agent" : "System";

// AFTER
meta.textContent = role === "user" ? "YOU >" : role === "agent" ? "AGT >" : "SYS >";
```

**Files:** `live.js` `createMessageElements` function

### 2.3 Add page header rule

Add a bottom border to `.page-header`:

```css
.page-header {
  padding-bottom: var(--space-md);
  border-bottom: 1px solid var(--dim);
  margin-bottom: var(--space-lg);
}
```

**Files:** `style.css`

### Phase 2 validation

- [ ] Page titles are uppercase geometric mono
- [ ] Body text is standard monospace
- [ ] Clear visual hierarchy: eyebrow < heading < body
- [ ] Speaker labels show "YOU >" / "AGT >" / "SYS >"
- [ ] Table headers are uppercase display font
- [ ] Form labels are uppercase display font

---

## Phase 3: Presence Layer

**Goal:** Add the ambient agent-state indicator visible on every page.

### 3.1 Add presence strip to shell

In `shell.html`, add a `<div>` for the presence indicator:

```html
<div class="presence-strip" data-presence="idle"></div>
```

Position it as a thin bar below the topbar (or as the topbar's bottom border):

```css
.presence-strip {
  grid-column: 2;
  grid-row: 1;
  align-self: end;
  height: 2px;
  background: var(--glow);
  opacity: 0.2;
}

.presence-strip[data-presence="idle"] {
  animation: presence-idle 4s ease-in-out infinite;
}

.presence-strip[data-presence="listening"] {
  animation: presence-listen 1.2s ease-in-out infinite;
}

.presence-strip[data-presence="speaking"] {
  animation: presence-speak 0.6s ease-in-out infinite;
}

.presence-strip[data-presence="thinking"] {
  animation: presence-think 2s linear infinite;
}

.presence-strip[data-presence="error"] {
  background: var(--warn);
  opacity: 0.6;
  animation: none;
}

@keyframes presence-idle {
  0%, 100% { opacity: 0.1; }
  50% { opacity: 0.25; }
}

@keyframes presence-listen {
  0%, 100% { opacity: 0.2; }
  50% { opacity: 0.7; }
}

@keyframes presence-speak {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.9; }
}

@keyframes presence-think {
  0% { background-position: -100% 0; }
  100% { background-position: 200% 0; }
}
```

For the `thinking` state, use a `linear-gradient` mask that sweeps left to
right, simulating a radar trace.

**Files:** `shell.html`, `style.css`

### 3.2 Drive presence state from JS

In `live.js`, update the `setVoiceState` function to also set the presence
attribute:

```js
function setPresence(state) {
  const strip = document.querySelector('.presence-strip');
  if (strip) strip.dataset.presence = state;
}
```

Map voice states to presence states:

```
disconnected  ->  idle (not error -- agent is still reachable via chat)
connecting    ->  thinking
connected     ->  idle
listening     ->  listening
```

When the agent is generating a response (typing indicator shown), set
presence to `thinking`. When agent is streaming speech, set to `speaking`.

**Files:** `live.js`

### 3.3 Non-live pages

On pages other than live, the presence strip defaults to `idle`. If SSE
activity events indicate agent processing, JS can update it, but this is
optional for the initial implementation. The strip being present and subtly
pulsing on every page is the core value.

### Phase 3 validation

- [ ] Thin glowing strip visible below topbar on all authenticated pages
- [ ] Strip pulses slowly when idle
- [ ] Strip pulses faster when listening (voice active)
- [ ] Strip animates during agent thinking/speaking
- [ ] Strip turns amber on error
- [ ] Respects `prefers-reduced-motion`

---

## Phase 4: Waveform & Voice Panel

**Goal:** Update the voice panel and waveform rendering to match the
monochrome aesthetic.

### 4.1 Waveform rendering

In `live.js`, update `drawWaveform` to use a single color instead of gradient:

```js
// BEFORE
const grad = ctx.createLinearGradient(0, h, 0, 0);
grad.addColorStop(0, "rgba(127, 208, 255, 0.8)");
grad.addColorStop(1, "rgba(255, 211, 110, 0.6)");
ctx.fillStyle = grad;

// AFTER
ctx.fillStyle = "hsla(165, 55%, 65%, 0.85)";
```

For phosphor decay, instead of clearing the canvas fully each frame, clear
with a semi-transparent fill to let previous frames fade:

```js
// BEFORE
ctx.clearRect(0, 0, w, h);

// AFTER
ctx.fillStyle = "hsla(165, 12%, 5%, 0.35)";
ctx.fillRect(0, 0, w, h);
ctx.fillStyle = "hsla(165, 55%, 65%, 0.85)";
```

This creates a natural phosphor-persistence trail on the bars.

**Files:** `live.js` `drawWaveform` function

### 4.2 Voice panel chrome

Update voice panel CSS to match the monochrome system:

```css
.waveform-canvas {
  background: var(--void);
  border: 1px solid var(--dim);
  border-radius: 0;
}

.voice-panel {
  border: 1px solid var(--dim);
  border-radius: 0;
  background: transparent;
}
```

**Files:** `style.css`

### 4.3 Voice state transitions

Replace the `hidden` attribute toggle on `.voice-panel-active` with a
`max-height` / `opacity` transition for smooth expand/collapse, consistent
with the agent card log pattern.

**Files:** `style.css`, `live.js` (minor -- switch from `hidden` toggle to
class/data-attribute toggle)

### Phase 4 validation

- [ ] Waveform bars are single-color (phosphor green-teal)
- [ ] Bars have visible decay trail
- [ ] Voice panel has sharp corners, no fill
- [ ] Start Call -> active transition is smooth
- [ ] Hang Up -> idle transition is smooth

---

## Phase 5: Focus, Accessibility, Motion

**Goal:** Add focus-visible styles, reduced-motion support, and ARIA
improvements.

### 5.1 Global focus-visible

Add to `style.css`:

```css
:focus-visible {
  outline: 2px solid var(--glow);
  outline-offset: 2px;
}

input:focus-visible,
textarea:focus-visible,
select:focus-visible {
  outline: none;
  border-color: var(--glow);
}
```

**Files:** `style.css`

### 5.2 Reduced motion

Add to `style.css`:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

**Files:** `style.css`

### 5.3 ARIA attributes

In `shell.html`:
- Add `role="navigation"` and `aria-label="Main"` to `.nav`
- Add `aria-current="page"` to the active `.nav-link` (done server-side in
  the Mustache template or via HTMX attribute)
- Add `aria-label` to icon-only buttons (mute, hangup, start call)

In `live.js`:
- Add `aria-label` to dynamically created buttons
- Ensure tool cards have `role="status"`

**Files:** `shell.html`, `live.js`

### 5.4 Normalize transitions

Replace all per-property transition declarations with a consistent pattern:

```css
transition: border-color 150ms ease-out, color 150ms ease-out, opacity 150ms ease-out;
```

Remove the current mix of `120ms ease`, `300ms ease`, and bespoke durations.

**Files:** `style.css`

### Phase 5 validation

- [ ] Tab through all interactive elements -- focus ring visible everywhere
- [ ] Enable `prefers-reduced-motion` in browser -- no animations
- [ ] Screen reader announces nav, chat messages, status changes
- [ ] All transitions feel uniform speed

---

## Phase 6: Responsive & Mobile

**Goal:** Add the 640px breakpoint and improve mobile voice experience.

### 6.1 Add 640px breakpoint

```css
@media (max-width: 640px) {
  .sidebar {
    /* collapse to minimal topbar with hamburger */
  }

  .nav {
    display: none;  /* hidden until hamburger toggled */
  }

  .main-content {
    padding: var(--space-md);
  }

  .conversation-panel {
    height: calc(100vh - 4rem);  /* less chrome overhead */
  }

  .chat-input-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    border-radius: 0;
    z-index: 10;
  }

  .two-up {
    grid-template-columns: 1fr;
  }
}
```

### 6.2 Hamburger toggle

Add a minimal hamburger button to the sidebar (hidden above 640px). On tap,
toggle the nav visibility. This requires:

- A `<button>` in `shell.html` inside `.sidebar`
- A small JS handler (inline or in `live.js`) to toggle a class
- CSS to show/hide `.nav` based on that class at the 640px breakpoint

### 6.3 Mobile voice bar

When a call is active on mobile, show a fixed bottom bar above the chat input
with mute/hangup controls and a compact waveform. This reuses the existing
voice panel content but repositions it.

### Phase 6 validation

- [ ] At 640px: sidebar collapses, hamburger shows
- [ ] Nav toggles on hamburger tap
- [ ] Chat input docked to bottom of screen
- [ ] Voice controls accessible during call on mobile
- [ ] Touch targets >= 44x44px

---

## Phase 7: PWA & Polish

**Goal:** Update PWA manifest, refine remaining details.

### 7.1 Update manifest

```json
{
  "name": "Mystic Horizon",
  "short_name": "Mystic",
  "display": "standalone",
  "start_url": "/dashboard/live",
  "background_color": "hsl(165, 12%, 5%)",
  "theme_color": "hsl(165, 12%, 5%)"
}
```

**Files:** `manifest.json`

### 7.2 Setup page treatment

Update inline styles in `setup.html` for the preparation panel to use the
monochrome system. The step list already uses icons and borders -- normalize
colors to `--glow` / `--warn` / `--dim`. The terminal output area uses
`--void` background.

### 7.3 Login page treatment

Minimal: single form with monochrome inputs, centered on `--void` background.
Already close to the target -- just needs radius and color updates (covered by
Phase 0).

### 7.4 Context strip on live page (optional)

Add a collapsible summary bar above the conversation feed showing: pending
action count, next calendar event, call status. Implemented as an HTMX
fragment:

```html
<div class="context-strip" data-expanded="0">
  <div hx-get="/dashboard/f/context-strip"
       hx-trigger="load, every 30s"
       hx-swap="innerHTML">
  </div>
</div>
```

This requires a new server-side fragment endpoint in `web.py`. Defer to a
later iteration if scope is a concern.

**Files:** `live.html` (or shell.html main content), `web.py`, `style.css`

---

## Execution Order Summary

```
Phase 0  CSS variables, font, spacing, radius     ~1 session
Phase 1  Remove gradients, shadows, chrome         ~1 session
Phase 2  Typography hierarchy, speaker labels      ~1 session
Phase 3  Presence layer                            ~1 session
Phase 4  Waveform & voice panel                    ~0.5 session
Phase 5  Focus, a11y, motion normalization         ~0.5 session
Phase 6  Mobile responsive                         ~1 session
Phase 7  PWA manifest, setup polish, extras        ~0.5 session
```

Phases 0-2 are the highest impact and should be done first as a unit. They
transform the visual identity. Phases 3-5 add the ambient and interactive
polish. Phase 6 addresses mobile. Phase 7 is cleanup and optional additions.

Each phase is independently testable. No phase requires reverting another.
