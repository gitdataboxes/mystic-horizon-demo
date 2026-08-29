# Mystic Horizon Design Manifesto

Retro-futuristic dark monochrome. The interface looks like it was designed by
serious people who expected it to last. CRT phosphor, not synthwave neon.
Oscilloscope traces, not gradient cards. Technology that earns trust by refusing
to decorate.

---

## Philosophy

**Light comes from content, not chrome.** The UI frame (sidebar, topbar,
borders) is near-invisible. Text and interactive elements emit the light. Active
elements glow brighter. Inactive elements recede into the void. There is no
material simulation. There is the screen and what is drawn on it.

**Monochrome means one hue, many values.** The entire UI is rendered in shades
of a single phosphor tone. Constraints reduce decisions. Fewer colors means
every brightness change carries meaning.

**The owner and agent are two people.** They chat and they call. The
conversation is a peer relationship, not a command interface. Pages (calls,
people, actions, calendar) are the agent's world made visible. Each is a real
interface into a real domain.

**Sharp geometry.** Vector displays draw straight lines. Panels, cards,
navigation, and buttons are rectangular. Small radii (2-4px) are acceptable at
very small scales (input fields, status dots) to prevent visual harshness, but
structural elements use sharp corners.

**Quiet until needed.** Animations communicate state change, not personality.
Everything moves at the same speed. Nothing bounces, nothing overshoots.
Transitions exist to say "something happened" without demanding attention.

---

## Palette

One hue. Five values. Everything derives from these.

```
--glow:     hsl(165, 55%, 65%)     /* brightest -- primary text, active elements       */
--mid:      hsl(165, 35%, 42%)     /* borders, secondary text, dividers                */
--dim:      hsl(165, 20%, 24%)     /* faint lines, inactive elements, disabled state   */
--surface:  hsl(165, 15%, 10%)     /* raised panels, cards, input backgrounds          */
--void:     hsl(165, 12%, 5%)      /* page background, the screen itself               */
```

### Semantic overrides

These break the monochrome rule deliberately. Use them only where meaning
requires a different hue:

```
--warn:     hsl(30, 40%, 50%)      /* errors, danger, destructive actions              */
--warn-dim: hsl(30, 25%, 20%)      /* warning backgrounds, error card tint             */
```

Success is not a separate color. Success is `--glow` at full brightness. The
system default state is healthy; only failures need chromatic distinction.

### Speaker tinting

Owner and agent share the same monochrome palette. Differentiation is
structural (alignment, framing) not chromatic. However, a subtle hue shift
provides additional separation at a glance:

```
--owner-hue:  140    /* slightly warmer green shift for owner messages      */
--agent-hue:  165    /* base hue, the default voice of the interface        */
```

These shifts are applied as border-color or subtle background tints on message
elements, not as separate palettes. The difference should be perceptible but
not prominent.

### Opacity scale

Opacity modulates brightness within the monochrome system:

```
1.0   active text, focused elements
0.7   secondary text, unfocused labels
0.4   disabled state, timestamps, meta
0.15  subtle backgrounds, hover tints
0.08  faintest tints, panel fills
```

---

## Typography

Two font stacks. The split reinforces the boundary between interface structure
and data output.

### Display (headings, page titles, nav labels)

Geometric sans with monospaced proportions. Used for anything that orients the
user: page names, section headers, button labels, navigation.

```
font-family: "Share Tech Mono", "Space Mono", ui-monospace, monospace;
text-transform: uppercase;
letter-spacing: 0.14em;
```

Candidates (in preference order): Share Tech Mono, Space Mono, Oxanium. These
read as stenciled-on-hull designators. If no web font is loaded, fall back to
system monospace -- the uppercase + letter-spacing still carries the vibe.

### Body (messages, form content, table data)

Clean monospace with good readability at small sizes. This is the terminal
voice -- where conversation text and dense data live.

```
font-family: "IBM Plex Mono", "JetBrains Mono", "SF Mono", Consolas, monospace;
letter-spacing: 0;
```

IBM Plex Mono is already in use and works well. Keep it.

### Type scale

Based on a 1.25 ratio from a 15px base:

```
--text-xxs:   0.68rem    /* 10.2px  micro-labels, line meta                */
--text-xs:    0.75rem    /* 11.3px  timestamps, captions, card log         */
--text-sm:    0.82rem    /* 12.3px  secondary body, form labels, nav       */
--text-base:  1rem       /* 15px    primary body, messages, table cells    */
--text-lg:    1.25rem    /* 18.8px  section headings (h2/h3)              */
--text-xl:    1.5rem     /* 22.5px  page titles (h1)                      */
--text-xxl:   2rem       /* 30px    hero/display (setup, login)           */
```

### Hierarchy rules

- Page titles: `--text-xl`, display font, uppercase, `letter-spacing: 0.14em`
- Eyebrow labels: `--text-xs`, display font, uppercase, `letter-spacing: 0.16em`, `--mid` color
- Section headings: `--text-lg`, display font, uppercase
- Body text: `--text-base`, body font, normal case
- Labels/captions: `--text-sm`, body font, uppercase, `--mid` color
- Micro text: `--text-xs`, body font, `opacity: 0.4`

---

## Spacing

8px base grid. All spacing values are multiples of 0.5rem (8px):

```
--space-xs:   0.25rem    /* 4px   tight internal gaps                     */
--space-sm:   0.5rem     /* 8px   between related elements                */
--space-md:   1rem       /* 16px  standard component gap                  */
--space-lg:   1.5rem     /* 24px  section separation                      */
--space-xl:   2rem       /* 32px  page padding, major sections            */
--space-xxl:  3rem       /* 48px  page-level breathing room               */
```

No values outside this scale. The current stylesheet uses 0.45rem, 0.65rem,
0.85rem, 0.95rem, 1.2rem, 1.25rem -- all of these get normalized to the
nearest grid value.

---

## Borders & Surfaces

### Structural borders

All structural elements (panels, cards, nav items, input fields) use:

```
border: 1px solid var(--dim);
```

On hover or focus: `border-color: var(--mid)`.
On active/selected: `border-color: var(--glow)`.

### Border radius

```
--radius-sm:  2px     /* inputs, small interactive elements               */
--radius-md:  4px     /* status dots, inline badges                       */
--radius-none: 0      /* panels, cards, buttons, nav links                */
```

Structural elements get `--radius-none`. The current 0.85rem-1.5rem radii are
removed entirely. Only small interactive elements where sharp corners create
visual noise (text inputs, the chat textarea) retain a minimal 2px radius.

### Panel treatment

Panels are transparent containers defined by their border, not fills:

```css
.panel {
  border: 1px solid var(--dim);
  background: transparent;
  /* no gradient, no box-shadow, no backdrop-filter */
}
```

When a panel needs visual separation from nested content, use
`background: var(--surface)` -- a single flat color, not a gradient.

---

## Presence Layer

A thin ambient indicator that communicates agent state across every page.
Implemented as a top-border glow on the topbar or a dedicated 3-4px strip
below it.

### States

```
idle:        slow brightness oscillation, 4s cycle, barely perceptible
listening:   rhythmic pulse, 1.2s cycle, medium brightness
speaking:    rapid pulse, 0.6s cycle, increased peak brightness
thinking:    left-to-right sweep animation, 2s cycle
error:       static --warn color, no animation
```

### Implementation

CSS-only using `@keyframes` on `border-image` or `background` opacity. The
presence strip uses `--glow` as its color. State changes are driven by a
`data-presence` attribute on the `<body>` or `.app-shell` element, toggled by
JS when voice bridge state changes.

On the live page, the presence effect is more visible (slightly taller strip
or higher opacity). On other pages, it is subtle -- peripheral awareness that
the agent is alive.

---

## Navigation

Sidebar nav items are flat text links with a left-edge active indicator:

```css
.nav-link {
  display: block;
  padding: var(--space-sm) var(--space-md);
  color: var(--mid);
  text-decoration: none;
  text-transform: uppercase;
  font-family: var(--font-display);
  font-size: var(--text-sm);
  letter-spacing: 0.14em;
  border: none;
  border-left: 2px solid transparent;
  background: transparent;
}

.nav-link:hover {
  color: var(--glow);
}

.nav-link.is-active {
  color: var(--glow);
  border-left-color: var(--glow);
}
```

No card-like containers, no hover lift, no background fills. Navigation is a
list of labeled destinations drawn on the screen.

---

## Agent Card

The agent card is a status readout block, not a floating card:

```
[dot] AGENT_NAME ----------- listening
```

- Horizontal layout: status dot, name (display font, uppercase), status text
  pushed to the right
- No border-radius, no panel background
- Expandable log below: monospace terminal output in `--mid`, status icons
  in `--glow` (success) or `--warn` (error)
- Collapsed by default when connected; auto-expands during connecting

---

## Chat Messages

### Owner messages (right-aligned)

```css
.msg-user {
  align-self: flex-end;
  border: 1px solid var(--dim);
  border-color: hsla(var(--owner-hue), 35%, 42%, 0.5);
  background: hsla(var(--owner-hue), 20%, 10%, 0.3);
}
```

### Agent messages (left-aligned)

```css
.msg-agent {
  align-self: flex-start;
  border: none;
  background: transparent;
}
```

Agent messages have no border or background. They appear as text printed
directly on the screen -- the agent's voice IS the interface. Owner messages
are framed to distinguish input from output.

### Speaker labels

Abbreviated terminal-style prefixes in `--text-xs`, uppercase, `--mid` color:

- Owner: `YOU >`
- Agent: `AGT >`
- System: `SYS >`

### Tool cards

Left-aligned, `border-left: 2px solid var(--dim)`. Running state uses
`--glow` left border. Completed fades to `opacity: 0.5`. Error uses
`--warn` left border. No border-radius.

---

## Voice Panel

The voice panel in the sidebar retains its current structure but adopts the
monochrome treatment:

### Waveforms

Single-color bar visualization in `--glow` against `--void`. No gradient fill.
Bars should have phosphor-decay behavior: values fade down over 2-3 frames
rather than snapping, mimicking CRT phosphor persistence.

```css
.waveform-canvas {
  background: var(--void);
  border: 1px solid var(--dim);
  border-radius: 0;
}
```

### Voice controls

Flat buttons matching nav style. "Start Call" is a standard button. "Mute" and
"Hang Up" are inline, with "Hang Up" using `--warn` text color.

### Call state transitions

Smooth height transitions between idle (just the Start Call button) and active
(waveforms + controls). No `hidden` toggle -- use `max-height` / `opacity`
animation consistent with the agent card log pattern.

---

## Pages

### Page header pattern

Every page uses this structure:

```html
<header class="page-header">
  <p class="eyebrow">DESIGNATION</p>
  <h1>Page title</h1>
</header>
```

The eyebrow is `--text-xs`, display font, uppercase, `--mid` color. The h1 is
`--text-xl`, display font, uppercase. A thin `1px solid var(--dim)` horizontal
rule sits below the header, separating it from content.

### Data tables

```css
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-base);
  font-variant-numeric: tabular-nums;
}

th {
  font-family: var(--font-display);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--mid);
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--mid);
  text-align: left;
}

td {
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--dim);
  color: var(--glow);
}
```

Row height is consistent. No alternating row colors. Divider lines only.
Column headers are structural designators, data cells emit the light.

### Detail views

Individual records (call detail, person detail, action detail) use bordered
rectangles containing labeled fields:

```
LABEL              value
LABEL              value
LABEL              value
```

Labels are `--text-xs`, uppercase, `--mid`. Values are `--text-base`, `--glow`.
Layout is a 2-column grid: label left, value right.

### Panels (home page, settings)

Bordered containers with no fill. Panel headings use the display font at
`--text-lg`. Content inside uses standard body typography.

---

## Forms

### Input fields

```css
input, textarea, select {
  border: 1px solid var(--dim);
  border-radius: var(--radius-sm);  /* 2px */
  background: rgba(0, 0, 0, 0.3);
  color: var(--glow);
  padding: var(--space-sm) var(--space-md);
  font-family: var(--font-body);
  font-size: var(--text-base);
}

input:focus, textarea:focus, select:focus {
  border-color: var(--glow);
  outline: none;
}
```

### Labels

Uppercase, `--text-sm`, `--mid` color, display font. The label reads as a
field designator.

### Buttons

```css
button {
  border: 1px solid var(--mid);
  border-radius: 0;
  background: transparent;
  color: var(--glow);
  padding: var(--space-sm) var(--space-lg);
  font-family: var(--font-display);
  font-size: var(--text-sm);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  cursor: pointer;
}

button:hover {
  border-color: var(--glow);
  background: hsla(165, 55%, 65%, 0.08);
}

button:disabled {
  color: var(--dim);
  border-color: var(--dim);
  cursor: not-allowed;
}
```

No hover lift (`translateY`). Hover is a border brightening and faint
background tint. Buttons are flat rectangles.

### Danger buttons

```css
.btn-danger {
  color: var(--warn);
  border-color: hsla(30, 40%, 50%, 0.4);
}

.btn-danger:hover {
  border-color: var(--warn);
  background: hsla(30, 40%, 50%, 0.08);
}
```

### Mode toggles (setup/settings)

The cloud/local radio toggle retains its pill shape but loses the gradient
fill. Selected state uses `border-color: var(--glow)` and
`background: hsla(165, 55%, 65%, 0.1)`. Unselected state uses `--dim` border
and transparent background.

---

## Setup & Onboarding

The setup wizard keeps its current flow (form -> preparation -> redirect) but
adopts the monochrome treatment:

- Setup shell: centered layout, no radial background gradients
- Step list: bordered items, status icons in `--glow` / `--warn`
- Active step spinner: `border-top-color: var(--glow)` (no cyan override)
- Terminal output: `--void` background, `--mid` text, `--text-xs` size
- Progress feels like a system boot sequence, not an onboarding wizard

---

## Motion

### Timing

All transitions use the same duration and easing:

```
--transition: 150ms ease-out;
```

This applies to: hover states, focus states, border color changes, opacity
changes, visibility toggles. One speed. No variation.

### Structural animations

Expand/collapse (agent card log, voice panel, step terminals) use:

```
max-height 250ms ease, opacity 200ms ease
```

### Keyframe animations

- **Presence pulse**: `opacity` oscillation at state-dependent speeds
- **Status dot pulse**: `opacity` 1.4s ease-in-out infinite (existing)
- **Spinner**: `rotate(360deg)` 0.8s linear infinite (existing)
- **Typing dots**: `translateY` bounce, 900ms (existing, keep as-is)

No new animation types. If it doesn't fit one of these four patterns, it
doesn't get animated.

### Reduced motion

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## Responsive

### Breakpoints

```
>= 920px    desktop     sidebar + main grid
< 920px     tablet      single column, sidebar stacks above
< 640px     phone       collapsed nav, full-width conversation
```

### Mobile behavior (< 640px)

- Sidebar collapses to a top bar with hamburger toggle
- Voice panel moves to a bottom-docked bar when call is active
- Chat input docks to viewport bottom
- Touch targets: minimum 44x44px on all interactive elements
- Page content is full-width with reduced padding (`--space-md`)

### Tablet behavior (< 920px)

- Sidebar stacks above main content (existing behavior)
- Voice panel details hidden until toggled (existing pill pattern)
- Two-column grids collapse to single column

---

## Accessibility

### Focus states

All interactive elements get a visible focus indicator:

```css
:focus-visible {
  outline: 2px solid var(--glow);
  outline-offset: 2px;
}
```

This is non-negotiable. Every button, link, input, and interactive element
must show where keyboard focus is.

### Color contrast

- `--glow` on `--void`: ~8:1 ratio (passes AAA)
- `--mid` on `--void`: ~4.5:1 ratio (passes AA)
- `--dim` on `--void`: ~2.5:1 ratio (decorative only, never used for text)
- `--glow` on `--surface`: ~5:1 ratio (passes AA)

Text content must use `--glow` or `--mid` only. `--dim` is for borders and
decorative elements, never for readable text.

### ARIA

- Navigation: `role="navigation"`, `aria-current="page"` on active link
- Agent card: `role="status"`, `aria-live="polite"`
- Chat feed: `aria-live="polite"`, `aria-atomic="false"`
- Icon-only buttons: `aria-label` required (mute, hangup, start call)
- Mode toggles: `role="radiogroup"` with `aria-label` (already present)
- Presence indicator: `aria-hidden="true"` (decorative)

### Keyboard

- `Enter` sends chat message (existing)
- `Shift+Enter` for newline in chat (existing)
- `Tab` navigates all interactive elements
- `Escape` could close expanded panels (future enhancement)

---

## File Organization

The design is implemented across these files:

```
mystic/_assets/
  dashboard/defaults/
    style.css           single stylesheet, CSS custom properties at top
    manifest.json       PWA manifest (theme_color updated to --void)
    pages/              page templates (unchanged structure)
  templates/
    shell.html          app shell with presence strip
    live.html           conversation page
    setup-shell.html    setup/login shell
    setup.html          onboarding wizard
    settings.html       settings forms
    login.html          token auth
  static/
    voice.js            voice bridge (waveform color changes)
    live.js             UI orchestration (presence state management)
```

No CSS preprocessor. No component library. No build step. The design system
lives entirely in CSS custom properties and consistent class usage.

---

## Naming Conventions

CSS classes use flat, descriptive names. No BEM, no utility classes:

```
.page-header          structural containers
.agent-card           component names
.agent-card-dot       component + child
.msg-user             component + modifier
.is-active            state (prefixed with is-)
.btn-danger           variant (prefixed with type)
```

Data attributes drive JS-controlled state:

```
data-state="listening"      agent connection state
data-expanded="1"           collapsible sections
data-presence="idle"        presence layer state
data-status="running"       tool card state
```
