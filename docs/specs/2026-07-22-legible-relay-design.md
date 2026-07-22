# Legible Relay - design

**Date:** 2026-07-22
**Status:** approved (brainstorm), pending implementation plan
**Scope:** Spec 1 of a 3-spec arc. Follow-ups (Reversible Relay, Frictionless
setup & settings, PR-watch, swarm exposure) are out of scope here - see the end.

## Why

Relay auto-approves safe permission prompts constantly while you are heads-down
elsewhere. What makes someone trust walking away is not a prettier UI - it is
knowing relay did the right thing. Today that knowledge is hard to reach: the
reason for every decision exists (`Decision.reason`, stored per tab as
`info.last_decision`, persisted to `~/.relay/audit.jsonl`) but is barely
surfaced; the mascot reports its *mood* but never its *substance* ("clearing..."
never becomes "cleared 12, nothing needs you"); and one flat alert sound
(`Sosumi`) fires for four different urgencies, so your ear cannot triage.

This spec makes relay's actions **legible and glanceable** by surfacing data it
already computes. It is deliberately additive: no new data model, no change to
how decisions are made or acted on (that is Spec 2, Reversible Relay).

## Goals

- A glance tells you relay has been productive *and* nothing needs you.
- The reason relay acted is visible where you are already looking (the panel).
- Different urgencies sound different, so your ear triages without looking.
- A pull-based summary of what relay did (`relay recap`).

## Non-goals

- No change to the classify/act pipeline (no hold window, no shadow-arm).
- No new notifications or sounds on *safe approvals* - approvals stay silent
  and ambient (calm posture, chosen during brainstorm).
- No TUI config editor here (it lands in Spec 3; sound keys are designed to
  slot into it).

## Posture (the load-bearing taste decision)

**Calm + ambient.** Frequent events (safe approvals) never make a sound, a
notification, or a mascot face-change - they only tick a counter. Only *rare
notable* events (danger caught, task done, worker message) get an active
signal. This single rule shapes every feature below.

## Architecture: the shared "what relay just did" spine

The only new machinery is a thin in-process layer on the `Watcher`. Every
feature reads from it (or from the audit log it mirrors).

- **`Watcher._approvals: int`** - session tally. Incremented once at the
  existing auto-approve site (`watcher.py` ~L442, the `audit.record(
  "auto-approved", ...)` success path). Scope: since this relay run started.
- **`Watcher._last_event: tuple[str, float] | None`** - `(kind, ts)` where
  `kind in {"approved", "danger", "done", "message"}`. Set at the existing
  call sites (auto-approve, danger-escalation NOTIFY, task/epic completion,
  worker-message delivery). Read by the TUI to drive a ~1.5s reaction pulse.
- **Consumers:** the TUI already holds a `Watcher` reference and subscribes via
  `on_change`; it reads `_approvals` and `_last_event` each render. `relay
  recap` reads persistent `audit.jsonl` instead (survives restarts).

No new files, no new storage. `_approvals` is in-memory (session scope);
persistent/day-scoped numbers come from the audit log.

### Event kind -> where it is set (existing sites)

| kind | set at | today |
|------|--------|-------|
| `approved` | auto-approve success (`watcher.py` ~L442) | records audit only |
| `danger` | dangerous-command NOTIFY (`watcher.py` ~L415-421) | plays `alert` |
| `done` | task/epic transition to done (swarm path) | *(no signal today)* |
| `message` | worker message delivered to you (`watcher.py` ~L662) | plays `alert` |

## Feature 1 - Inline "why"

**What:** show the decision *reason* (not just the command) where the panel
already shows per-tab detail.

**Where:** the row-highlight detail footer (`app.py` ~L851) currently derives a
"why" string from `info.last_command`. Extend it to lead with
`info.last_decision`:

```
cleared: safe permission prompt - grep foo src/
```

For a NOTIFY/danger tab the same footer reads e.g. `escalated: dangerous
command - git push --force`. `info.last_decision` is already populated
(`watcher.py:363`); this is presentation only.

**Calm:** no notification or sound on approval. The mascot tally is the ambient
headline; this footer is the pull-detail when you highlight a row.

## Feature 4 - Mascot barometer

Two additions to the existing 5-state creature (`mascot_state` /
`mascot_face_big` in `app.py`). No new states in the priority ladder; these
augment existing states and add two short-lived reaction overlays.

**A. Substance in speech.** Thread the `_approvals` tally into
`mascot_face_big` (new keyword arg, default 0 so existing tests/callers stand).
Guarding and working phrases gain a tally variant:

- guarding: rotate in `12 cleared · quiet`, `guarding 3 · 12 done`,
  `nothing needs you (12)` alongside the current mood lines.
- working: `clearing · 12`.

When the tally is 0 the phrasing falls back to today's mood-only lines (a fresh
relay does not claim work it has not done - same honesty rule as the working
verbs).

**B. Momentary reactions (~1.5s).** When `_last_event` is fresh, overlay a
reaction frame on top of the base mood, then decay back:

- **done** (task/epic complete): celebration frame (`^  ^` eyes, `✓` on screen,
  smile, `★` beacon, `task done ★`) + play the `done` sound (see Feature 5).
- **danger** (caught a dangerous command): a flinch frame (`O  O` eyes, `!` on
  screen, open mouth, `!` beacon, `whoa - danger`) shown the instant it is
  caught, then control passes to the existing ALARMED state (which persists
  while it awaits you).

Reaction TTL: ~1.5s (3 reactor ticks at 0.5s). `approved` and `message` do
**not** get a face reaction (approved is too frequent; message is covered by its
sound + the existing escalation surfaces) - they may tick the beacon at most.
Frequency guard: reactions are edge-triggered on a *new* `_last_event.ts`, so a
burst of approvals cannot chain reactions.

Copy uses ASCII hyphens only (no em-dash), per repo rule.

## Feature 5 - Sounds as config

`[sounds]` grows from 2 keys to 4, backward-compatible (existing configs keep
working; new keys use defaults):

| Key | Fires on | Default | Status |
|-----|----------|---------|--------|
| `danger` | a session about to run a **dangerous** command | `/System/Library/Sounds/Basso.aiff` | new |
| `alert` | needs-a-look: real question, stale, spawn-arm, error | `/System/Library/Sounds/Sosumi.aiff` | unchanged |
| `message` | a swarm **worker messaged / escalated** to you | `/System/Library/Sounds/Tink.aiff` | new |
| `done` | a task/epic **completed** | `/System/Library/Sounds/Glass.aiff` | revived (was dead) |

- Every key overridable in `[sounds]`; **empty string = silent** for that
  category.
- Route each existing `notify_mac(...)` call to the correct key: the
  dangerous-command NOTIFY uses `danger`; worker escalation/message uses
  `message`; stale / spawn-arm / audit-error keep `alert`. Wire `done` to the
  task/epic-complete path (currently plays nothing).
- `Config` gains `danger_sound` and `message_sound` fields with the defaults
  above; `Watcher` stores them like the existing two.
- Keys are independent, previewable, silenceable strings - designed so the
  Spec 3 Settings screen can render a per-key picker with a "play sample"
  button on top of them with no rework.

## Feature 6 - `relay recap`

**What:** a read-only CLI command that aggregates `audit.jsonl` for a window.

- `relay recap` - default window "today" (local midnight to now).
- Summary line: `cleared 47 · woke you 3× · 2 tasks done · 1 stall caught`.
- Optional per-tab breakdown (top tabs by approvals).
- Derived from audit verdicts: `auto-approved` -> cleared; `escalated` ->
  woke you; `delivered`/task states -> tasks done; stale notices -> stalls.
- Added as `cmd_recap` in `cli.py` with a `recap` subparser (mirrors
  `cmd_doctor`: reads only, never mutates).

**Also:** print the same one-line summary on TUI quit (the watcher already runs
a clean shutdown path; emit one recap line there).

## Testing

All logic stays in pure, iTerm2-free helpers, matching `test_statusbar.py` /
`test_watcher.py`:

- **Mascot:** `mascot_face_big` with `approvals=` produces the tally phrasing;
  tally 0 falls back to mood-only lines; a fresh `_last_event` yields the
  done/danger reaction frame and decays after TTL. Frame geometry stays aligned
  (screen interior stays 6 chars).
- **Sounds:** a routing table test - each event kind maps to the expected
  config key; empty key = no sound; unknown key falls back safely.
- **Recap:** aggregation over a fixture `audit.jsonl` yields the expected
  counts; window filtering (today vs older) is correct; empty log yields a
  sane "nothing yet" line.
- **Watcher spine:** `_approvals` increments once per auto-approve;
  `_last_event` is set with the right kind at each site; edge-trigger guard
  (repeated same-ts event does not re-fire).

## Rollout / risk

- Purely additive; default posture is calm, so no new noise unless a rare
  notable event occurs.
- The two new sounds have defaults but are fully overridable/silenceable, so a
  user who dislikes them sets the key empty.
- `_approvals` / `_last_event` are best-effort in-memory state; a miss degrades
  to today's behavior (no tally, no reaction) - never breaks the watcher loop,
  consistent with the existing "status bar must never break the watcher" rule.

## Out of scope (sequenced follow-ups)

1. **Reversible Relay** (Spec 2): hold/veto window before wild/insane injects;
   shadow-arm (decide without acting, reusing the `would-approve` verdict).
2. **Frictionless setup & settings** (Spec 3): `relay doctor --fix`; guided
   first-run; **TUI config editor** (the Settings screen that consumes the
   Feature 5 sound keys with a play-sample picker, plus the `relay doctor`
   checklist as a Diagnostics tab).
3. **PR-watch skill** (standalone): scheduled `gh`-driven PR poller; notify on
   changes-requested, confirm-to-merge on approved+green+mergeable. Never
   silent auto-merge. Promote into Relay as "PR arm levels" only if it earns
   its keep.
4. **Swarm exposure** (parked): revisit after Specs 1-2 land and the
   single-user trust core is airtight.
