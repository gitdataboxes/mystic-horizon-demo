# Mystic Horizon Knowledge Graph

A living map of the agent's relational world. Who the agent knows, through what
channels, and how actively. The graph shows *structure*; facts, action details,
and interaction transcripts are *content* and live in the SEARCH sidebar — not
painted onto the graph.

Same palette, same type, same motion grammar as the rest of the dashboard.
Reuses the DESIGN.md tokens without additions.

---

## Purpose

A glance should answer:

- Who does the agent know?
- Through what channels do they interact?
- How active is each relationship right now?
- What does the agent itself feel like, as a being?

Questions the graph does *not* answer — content belongs in SEARCH:

- What does the agent know *about* Alex? → person page / search
- What was said in the last call? → interaction thread
- What is the agent working on right now? → activity feed / actions list

---

## Topology

- **Agent** at the center.
- **Person** nodes in a ring around the agent.
- **Channel edges** connect the agent to each person — one strand per channel
  in use (phone, SMS, chat).
- No person↔person edges. No floating fact nodes. Facts and actions are
  content, not graph citizens.

```
                   person
                     |
       person  ===channels===  AGENT  ===channels===  person
                     |
                   person
```

Each `===channels===` is up to three parallel strands, each present only if
that channel has been used for that person.

---

## Layout

### Position

**Fixed radius. Fixed angle.** Every person node sits at the same distance
from the agent. Angular position is determined by *first-interaction order* —
the date the agent first heard from that person. Once assigned, a person's
angle is permanent. New people take the next available angular slot, expanding
the constellation outward in time but never disturbing existing positions.

The result: a person's slot becomes muscle memory ("Alex is at 2 o'clock,
always") while the ring as a whole tells a chronological story of the agent's
relational life.

### Filter & density

Default view: **top N by recent activity**. Inactive people are hidden but
retain their angular slot — the visible constellation has gaps, and the gaps
themselves carry meaning ("someone used to be here").

A small floating control panel in the graph pane's corner adjusts:

- **N** — how many people to show
- **Window** — time range that counts as "recent"

Retro-futuristic chip pair in the DESIGN.md vocabulary. Always visible,
never a modal, never a dropdown.

---

## Agent node

Not a label. The most visible instantiation of the agent as a being.

**Alive and stateful.** The node visually reflects what the agent is doing
right now:

- `idle` — slow breathing, barely perceptible oscillation
- `listening` — subtle inbound shimmer
- `thinking` — internal swirl
- `speaking` — outward pulse synchronized to the HUD waveform amplitude

**A window, not a glyph.** Self-knowledge (identity, soul, journal depth) is
rendered *inside* the node as internal strata. The viewer looks *into* the
agent, not at a symbol of it. CRT phosphor interior against `--void`.

State is driven by the same `data-presence` attribute that drives the
presence strip (see DESIGN.md: Presence Layer). One source of truth for agent
state across the whole dashboard.

---

## Person nodes

A circle with a halo, rendered in `--glow`.

- **Fact count** → halo brightness / size ("deep knowledge here")
- **Pending action count** → small badge on the node ("unresolved thread")
- **Identification state** → named: solid outline. Unnamed: dashed outline,
  "?" glyph, desaturated.

### Unnamed people

A phone number that called once but hasn't been identified is still a Person
row. The graph renders it with a dashed outline and a "?" where initials would
sit. Clicking the "?" converts it into an inline rename field — type a name,
press Enter, the node resolves.

### Spam

Calls that the agent classifies as spam carry a SPAM label on the call
record. Person nodes created from spam-only interactions are filtered out of
the graph. Call records remain in the data and are reachable through the
normal call log — only the graph hides them.

---

## Channel edges

One edge per (agent, person, channel) where the channel has been used.

- **Thickness** — total channel activity, recency-decayed. A relationship
  that used to be active but has gone quiet thins over time.
- **Glow** — recency. Recent = `--glow` bright; dormant = `--mid` faded.
- **Signal pulses** — discrete events, not a continuous stream.

### Signal pulses

Each text message or completed voice turn fires **one** signal pulse that
travels the edge in the Direction of that message:

- Inbound message / turn → pulse from person → agent
- Outbound message / turn → pulse from agent → person

The edge is calm between messages. The conversation's rhythm becomes legible
as discrete beats — turn-taking is visible, long pauses are visible, rapid
back-and-forth is visible. A steady stream would say "something is happening."
Per-turn pulses say *what* is happening, at the cadence it is happening.

### Drilling in

Clicking an edge opens the interaction thread for that (person, channel) pair
in the SEARCH sidebar.

---

## Live interaction choreography

When an interaction begins, the graph responds in order:

1. **Node materializes.** If the caller or sender is new, a Person row is
   created and a node appears at the newest angular slot. Named people already
   on the graph simply light up.
2. **Edge lights cold → hot.** The relevant channel edge fades from its
   dormant glow to active brightness.
3. **Per-turn pulses fire.** Each message or completed voice turn emits one
   signal pulse along the edge in its Direction.
4. **Badge appears.** If the agent commits to a pending action during the
   interaction, the person node's badge increments.
5. **Edge settles.** When the interaction ends, edge thickness updates to
   reflect the new activity total, then fades back toward baseline as recency
   decays.

Nothing bounces. Nothing overshoots. Transitions follow the DESIGN.md motion
grammar — the graph breathes at the same tempo as the rest of the interface.

---

## Search ↔ graph coupling

Content and structure are two views of the same data.

- **Click a node or edge** → SEARCH sidebar filters to that entity or thread.
- **Type in SEARCH** → matching nodes and edges highlight on the graph;
  non-matches recede to `--dim`.

The graph is a shape. The sidebar is the details. Neither is complete alone.

---

## Out of scope

Declared explicitly so the design stays coherent:

- **Person↔person fact edges.** Relational facts ("Alex is Morgan's brother")
  are content, not structure. They surface through SEARCH.
- **World-facing actions.** Reminders, calendar blocks, and self-directed
  tasks with no human counterpart live in the activity feed, not the graph.
- **Fact-clustered-by-topic view.** A different question ("what does the
  agent know about my finances?") deserves a different visualization. Not v1.
- **Spam Person rows on the graph.** Agent-labeled spam is filtered out; call
  records remain in the data.

---

## Load-bearing principles

- **Structure vs. content.** The graph shows *who*, *through what*, and *how
  actively*. Everything else is content and lives in SEARCH.
- **Stability as a feature.** Angular slots are permanent. Radius is fixed.
  Muscle memory beats novelty.
- **One signal per channel.** Edge thickness, edge glow, signal pulses, node
  halo, node badge — each carries a single meaning. No overloading.
- **Discrete beats over steady streams.** Pulses tied to real events
  (messages, turns) always beat generic "something is happening" animation.
