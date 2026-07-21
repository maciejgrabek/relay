# TUI visuals - design

Date: 2026-07-21
Status: approved (brainstormed with Maciej, mockups accepted)

The deferred visuals batch: make both views answer "what's going on and what
needs me?" at a glance. All rendering stays pure (swarm.py / app.py helpers);
watcher only passes through facts it already knows.

## Swarm view (TAB)

1. FLEET bar (top line, spans projects): worker counts by state - busy (owner
   of a doing task), blocked (owner of a blocked task, not busy), idle (the
   rest) - plus armed counts by mode (from the sessions table's persisted
   `mode`), stale count, and queued message count.
2. INTERACTIONS: per unordered pair of names, aggregated over the fetched
   message history: sent/received counts (first name's perspective,
   coordinator listed first), last kind, age of last message, and a `!!` flag
   when the last kind is blocked/escalation. Capped at the 6 most recently
   active pairs.
3. Colors + progress bars: the swarm Static flips to Rich markup with ALL
   dynamic text escaped. Feed colors by kind: done green, blocked yellow,
   escalation red, wake dim. Epics render `X/Y` as a `#### ....` style bar
   (10 cells, filled = done share).
4. Heartbeat: `last-activity age` per roster row (watcher activity timestamp,
   DB last_seen fallback); a stale session's row shows the stale glyph and
   renders red.

`render_swarm(sessions, tasks, messages, now, width, stale=frozenset(),
activity={})` - new optional args; returns markup text (callers switch the
Static to markup=True).

## Control view

5. NEEDS ACTION strip (revised after first hands-on feedback): sessions
   awaiting a prompt decision, stale, or blocked appear as DUPLICATE rows in
   a strip at the top; the main list below keeps its stable tab order ALWAYS
   (rows moving between sections broke spatial memory - explicitly rejected).
   The duplicate disappears once actioned; the original row never moves.
   Arrow keys walk continuously (strip rows, then the whole list; dividers
   skipped) in both directions; cursor restore prefers the occurrence
   nearest to where the cursor was. Strip rows keep every interaction.
   No attention rows -> no dividers at all.
6. Header attention counts: `· N awaiting · N stale · N msgs queued` appended
   to the existing units/armed/approvals header (each part only when > 0).
7. Feed context line: first line of the live-feed pane names the selected
   session, its mode, and - when it is being held - why:
   `bff-worker · SAFE · AWAITING: "<command>" escalated (dangerous)`.
8. Heartbeat column: `LOC` column becomes the age since the session's screen
   last changed (LOC's job - finding the tab - is what `n` does better).
9. LAST DIRECTIVE color: red when that command was escalated, default when
   auto-approved/none.

## Out of scope

- No new keybindings, no new columns beyond the LOC swap.
- No historical charts/sparklines; ages and counts only.
- Interaction map stays name-pair based (no graph drawing).

## Testing

Pure helpers in swarm.py (fleet counts, pair aggregation/ordering/flag/cap,
bar text, heartbeat ages, markup escaping incl. hostile `[red]` bodies) in
test_swarm.py. Control-view partitioning/marker logic as pure app.py helpers
tested in test_app.py, plus a headless run_test that renders both views with
hostile strings and exercises cursor navigation across the new dividers.
