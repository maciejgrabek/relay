# Tab-Side Mode Switching - Design Draft

**Date:** 2026-07-15
**Status:** Draft (not yet planned or implemented)
**Origin:** docs/IDEAS.md idea 1

## Summary

Let the human flip the CURRENT iTerm2 tab's arm level (off/safe/wild/insane)
without switching to the relay TUI, via two iTerm2-native affordances:

1. a **key binding** ("Invoke Script Function") that cycles the mode of the
   tab you are looking at, and
2. an optional **custom status bar component** that shows the current tab's
   mode and cycles it on click.

Both are backed by RPC functions the already-running relay watcher registers
over its existing iTerm2 Python API connection. There is **no new IPC
channel, no DB row, no helper process**: an RPC invocation lands directly in
the watcher's asyncio loop and calls the same `toggle()` / `set_mode()` the
TUI's `Space` key uses.

The channel choice is the core design decision, driven by the safety
constraint from the idea: **mode changes are a human act; the affordance must
be un-spoofable from inside the session**. RPCs qualify because they can only
be triggered by a real key press or mouse click in iTerm2's chrome - things a
process running inside the terminal cannot synthesize. The `mode_request` DB
row floated in IDEAS.md is explicitly rejected (section 6).

## 1. UX

### 1.1 Key binding (primary)

The watcher registers an RPC per action:

- `relay_mode_cycle()` - off -> safe -> wild -> insane -> off (same cycle
  and same code path as the TUI's `Space`)
- `relay_disarm()` - straight to off from any level (the "oh no" key; no
  cycling through insane to get out of wild)

The user binds keys once in iTerm2 (Settings > Keys > Key Bindings > action
"Invoke Script Function"), e.g. `Cmd-Shift-A` -> `relay_mode_cycle()`.
Each RPC declares `session_id=iterm2.Reference("id")`, so iTerm2 fills in
the session the key was pressed in - the function applies to the tab that
has focus, no argument needed in the binding.

Suggested bindings (documented in README, not auto-installed - we never
touch the user's key map):

| binding (suggested) | function            | effect                      |
| ------------------- | ------------------- | --------------------------- |
| `Cmd-Shift-A`       | `relay_mode_cycle()`| cycle off/safe/wild/insane  |
| `Cmd-Shift-Escape`  | `relay_disarm()`    | jump to off                 |

### 1.2 Status bar component (secondary, optional)

A `StatusBarComponent` registered by the watcher, added by the user via
iTerm2's status bar setup UI (Settings > Profiles > Session > Configure
Status Bar). Per session it renders the mode the watcher actually holds
(e.g. `◉ SAFE`, `▲ WILD`, `○ MANUAL`), reusing the fixed glyph vocabulary
from `iterm/titles.py`. Click = same effect as `relay_mode_cycle()`.

The component's value callback runs in the watcher process and answers from
`SessionInfo.mode` directly - the authoritative in-process state. It does
NOT read or write iTerm2 user variables (section 6.2), so the display cannot
be forged by escape sequences from inside the session.

### 1.3 Feedback

- **Immediate:** the mode change takes effect on the watcher's next gate
  evaluation (same as a TUI `Space`; `_last_prompt_id` is reset so the
  current prompt is re-evaluated under the new mode).
- **Visible:** the `[titles]` prefix feature (if enabled) rewrites the tab
  title on the next tick; the status bar component (if installed) updates on
  its next refresh; the TUI MODE column reflects it live.
- **Audible:** escalations (any change that raises the level, e.g. safe ->
  wild) play the alert sound once. De-escalations and off are silent.
  Rationale: arming-up widens the blast radius and should be hard to do by
  accident without noticing; a stray `Cmd-Shift-A` on the wrong tab must not
  be silent.
- **Audited:** every tab-side mode change appends an audit row (section 5.3).

### 1.4 Parity with the TUI

Tab-side switching is the SAME state, not a parallel one. There is exactly
one holder of arm state: `SessionInfo.mode` in the watcher process. TUI
`Space`, spawn pre-arm, and tab-side RPCs all mutate that one field. Nothing
new to reconcile.

Full cycle parity is deliberate: the tab-side cycle reaches wild and insane,
same as `Space`. A cap (e.g. "tab-side can only reach safe") was considered
and dropped - it forks the mental model of one cycle, and the person
pressing the key binding is by construction the same person who could reach
the TUI anyway. The escalation sound covers the fat-finger risk.

## 2. Architecture

### 2.1 Where the code lives

- **New module `iterm/tabmode.py`** - registration and handlers:
  `async register(connection, watcher, cfg)` sets up the two RPCs and the
  status bar component. Handler logic (which mode is next, is this an
  escalation, what to render) is pure functions, unit-testable without
  iterm2; only the registration glue imports iterm2.
- **`iterm/watcher.py`** - calls `tabmode.register(...)` once in `start()`
  after the connection is up, gated on config (section 4). No other change:
  the handlers call the existing `toggle()` / `set_mode()`.
- **`iterm/titles.py`** - unchanged; tabmode reuses its glyph/word
  vocabulary for the status bar text so mode has ONE visual language.

### 2.2 Flow

```
key press in iTerm2 tab X            click on status bar of tab X
        |                                        |
        v                                        v
iTerm2 invokes registered RPC (watcher's existing connection)
        |
        v
tabmode handler (watcher asyncio loop, same thread as gates)
  - resolve session_id -> watcher.sessions[sid]; unknown -> note + ignore
  - compute next mode (pure)
  - audit.record("mode-change", ...)   [LOG BEFORE ACT]
  - watcher.set_mode(sid, next)        [same as TUI Space]
  - escalation? -> play alert sound
        |
        v
next tick: gates use new mode; titles + status bar + TUI show it
```

No locking needed: the RPC handler runs on the same asyncio loop as the
watcher's tick, so mode mutation is single-threaded, same as today's TUI
actions.

### 2.3 Lifecycle - "tool on === TUI open" holds

RPCs and the status bar component are registered when the watcher starts and
vanish when the process exits (iTerm2 drops registrations with the
connection). With relay closed:

- the key binding shows iTerm2's standard "unknown function" toast - honest,
  no silent no-op, and nothing on the system can arm anything;
- the status bar component displays iTerm2's stale/unavailable state.

No daemon is added; nothing runs that wasn't running before.

### 2.4 Dry-run

Mode changes are allowed in `--dry-run`, matching the TUI (`Space` works in
dry-run today): arming in dry-run only changes what gets LOGGED as
`would-approve`, never causes injection. The audit verdict stays
`mode-change` with a `dry-run` marker in the reason, keeping the "dry-run
mutates nothing outside the process" guarantee intact (the audit log is
relay's one file, and dry-run already writes `would-*` rows to it).

## 3. Security constraints (the actual point)

### 3.1 The invariant

**A process running inside a session must not be able to change any
session's arm level - including its own.** The whole product rests on modes
being set by a human: an armed session auto-approves commands, so
self-arming = a Claude session (or anything it runs) granting itself
approval powers.

### 3.2 Why RPCs satisfy it

An "Invoke Script Function" key binding fires only on a physical key event
handled by iTerm2's UI layer; a status bar click fires only on a mouse event
in iTerm2's chrome. A program inside the terminal can write bytes to the
tty (including escape sequences) and spawn processes - it cannot deliver
key/mouse events to the iTerm2 application. There is no escape sequence
that invokes a script function. The trigger is therefore a human act by
construction, which is exactly the constraint.

What remains in the threat model (unchanged by this feature): a local
process outside the session sandbox running as the user (it could script
System Events, edit the DB, or type at terminals - the documented local-
trust boundary), and the human themselves being socially engineered by
session output ("please press Cmd-Shift-A") - mitigated by the escalation
sound and audit trail, not solvable at this layer.

### 3.3 Pre-existing gap this design surfaced (fix first)

The spawn pre-arm channel (`sessions.arm_request`, applied in
`watcher._swarm_refresh_registry`) currently violates 3.1: the watcher
applies `arm_request` to ANY registered session it currently sees, every
time the column is non-empty - not just to freshly spawned tabs. Any local
process that can write `~/.relay/relay.db` can therefore arm any session,
and notably **a session armed `safe` can escalate itself to `insane`**: a
one-line `sqlite3` UPDATE on its own row classifies as safe under
`lib/danger.sh` (sqlite3 is not a dangerous pattern), gets auto-approved,
and the watcher obeys the row on its next tick.

This ships today, independent of tab-side switching - but it makes the
"un-spoofable" property of this feature meaningless until closed, so fixing
it is a **prerequisite, not a nice-to-have**:

- Honor `arm_request` only for sessions the watcher has NEVER seen before
  (true spawn pre-arm): track first-seen; if the session was already present
  in `self.sessions` on a previous tick with the request absent, ignore the
  request, clear it, and notify (sound + note) that a runtime arm request
  was rejected.
- Keep `relay spawn --arm` working unchanged - the request is written before
  the tab exists, so it is always "never seen" when first applied.
- Do NOT add a `relay arm` CLI verb, ever. The CLI runs inside sessions;
  an arm verb is self-arming as a feature.

### 3.4 Display integrity

The status bar component and tab-title prefixes render from watcher state,
never from iTerm2 user variables. A session CAN set user variables via
OSC 1337 `SetUserVar`, so any display channel reading them could be forged
(e.g. showing MANUAL while armed insane). By answering the component's
value callback from `SessionInfo.mode` in-process, the display is exactly
as trustworthy as the watcher itself.

### 3.5 Audit

Manual TUI keypresses are deliberately not audited today; tab-side changes
ARE (verdict `mode-change`, session title, old -> new mode, source
`keybind` / `statusbar`). Rationale: this is a new trigger surface whose
un-spoofability rests on an argument about iTerm2 internals, not on relay's
own code. If that argument is ever wrong (an iTerm2 bug, a future
"synthesize keystroke" API), the audit trail is how you find out what got
armed and when. Cheap insurance, same LOG BEFORE ACT contract as approvals:
if the audit write fails, the mode change is refused.

## 4. Configuration

One new key in the existing `[titles]`-style pattern:

```ini
[tabmode]
enabled = off          ; off | on   (default off)
```

Opt-in, like titles: when off, `tabmode.register()` is never called, so
nothing is registered - zero new surface for existing users. The suggested
key bindings live in the README; relay never edits the user's iTerm2 key
map or status bar layout (both are the user's, and both survive relay
uninstalls untouched).

Deliberately not configurable: the cycle order (parity with `Space`), the
RPC names (they are the public contract user key bindings point at), and
the escalation sound (it is a safety feature, not a preference; users who
want silence can pick a silent alert sound globally).

## 5. Failure modes and edge cases

| case | behavior |
| ---- | -------- |
| relay not running | key binding -> iTerm2 "unknown function" toast; nothing armable |
| RPC fires for a session the watcher does not track (brand-new tab, mid-enumeration) | ignore + `_note`; no crash, no default arm |
| audit write fails during mode change | mode change refused, notification fired (same as approval path) |
| two rapid presses | both handled in order on the one loop; cycle advances twice - matches pressing Space twice |
| session closes between press and handler | `sessions.get(sid)` misses -> ignore |
| iTerm2 without Python API RPC support | registration failure is caught at startup, one warning note, feature inert |
| user binds a key but leaves `enabled = off` | "unknown function" toast (registration never happened) - README documents the pairing |

## 6. Rejected alternatives

### 6.1 `mode_request` DB row (the IDEAS.md sketch)

A `mode_request` column/row the watcher picks up on its next tick mirrors
the existing `arm_request` plumbing - which is exactly why it fails: the DB
is writable by every local process including the sessions themselves (the
relay CLI is even added to spawned workers' PATH). A DB row cannot carry
provenance; "who wrote this" is unknowable at the SQLite layer. It would
institutionalize the very hole 3.3 closes. Rejected outright, not deferred.

### 6.2 iTerm2 user variables / OSC escape channel

`SetUserVar` escape sequences can be emitted by the session itself - i.e.
by Claude. Spoofable by definition. Rejected for both trigger and display.

### 6.3 Standalone helper script / AutoLaunch iTerm2 plugin

A separate always-on Python script could register the same RPCs, but it
breaks "tool on === TUI open" (a second process with its own lifecycle),
needs its own channel back to the watcher (which lands you in 6.1), and
arms nothing when relay is closed anyway. All cost, no capability.

### 6.4 Auto-installing key bindings or status bar layout

Editing the user's iTerm2 preferences programmatically is rude, fragile
across iTerm2 versions, and survives uninstall as residue. Suggest, never
write.

## 7. Out of scope

- Changing another tab's mode from a tab (the affordance is current-tab
  only; cross-tab control is what the TUI is for).
- Arm-all / disarm-all from a tab (same reason - it's a fleet action).
- Any CLI verb that changes arm state (see 3.3 - self-arming as a feature).
- Touch Bar / menu bar / Stream Deck affordances (same RPC mechanism would
  serve them later; nothing here blocks it).
- Configurable cycle order or per-level key bindings (`relay_set_mode(mode)`
  can be added later behind the same security argument if wanted).
- Windows/Linux terminals - relay is iTerm2-native by charter.

## 8. Testing strategy

- **Pure logic** (`iterm/test_tabmode.py`): next-mode computation, escalation
  detection (off->safe yes, wild->safe no), status bar text rendering
  against the titles vocabulary, unknown-session handling - no iterm2
  imports, same pattern as test_titles.py / test_gates.py.
- **Prerequisite fix** (3.3): extend test_swarm/test_db coverage - arm_request
  honored on first sight, ignored + cleared + notified for an already-seen
  session.
- **Live checklist** (manual, like the existing "what's verified" gap):
  register with `enabled = on`, verify key binding cycles the focused tab and
  the TUI reflects it; verify toast with relay closed; verify audit rows;
  verify a `sqlite3` self-arm attempt is rejected and notified.
