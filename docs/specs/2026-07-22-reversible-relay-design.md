# Reversible Relay - design

**Date:** 2026-07-22
**Status:** approved (brainstorm), pending implementation plan
**Scope:** Spec 2 of the roadmap arc (Spec 1 = Legible Relay, shipped). This
spec adds two ways to STOP or WITHHOLD relay's actions. Follow-ups (Frictionless
setup & settings, PR-watch, swarm exposure) remain out of scope.

## Why

Legible Relay made relay's actions visible. Reversible Relay makes them
*stoppable* and *withholdable* - the other half of trust. Two features:

1. **Global pause** - an "oh no, stop" button. When you glance over and see
   something going wrong, one key freezes relay's *hands* (auto-approvals and
   swarm message deliveries) while keeping its *eyes* open (it still watches,
   shows state, and warns you on danger). It holds until you resume.
2. **Shadow-arm** - a per-tab dry-run. Arm a tab in "shadow" and relay
   classifies and records what it *would* do, without acting - so you calibrate
   trust on a real session before letting it act, while your other armed tabs
   keep acting for real.

This spec touches relay's *act* path (unlike Legible Relay, which was purely
presentational), so correctness and fail-safe behavior are the priority.

## Non-goals

- No per-approval veto/countdown (rejected in brainstorm: it fights the
  walk-away premise and needs a keyboard channel relay does not own on the
  target tab). Pause is the global alternative.
- No auto-resume of a pause (rejected: a deliberate stop must not silently
  re-enable acting). Pause holds until the human resumes.
- Shadow does not shadow wild/insane (too crude to be worth trust-testing) -
  it mirrors the *safe* pipeline only.

## Feature 1 - Global pause

### Behavior

- **Toggle:** `p` in the panel toggles `Watcher.paused` (a live bool). `p`
  again resumes. No timeout, no persistence - a freshly started relay is never
  paused.
- **Freezes (the hands):**
  - Auto-approval: the inject (`async_send_text("\r")`) in `_handle`'s approve
    path is skipped while paused.
  - Swarm delivery: `_deliver` returns early while paused (the message stays
    queued and retries once resumed).
- **Keeps (the eyes):** the poll loop, screen reads, displayed state, and
  danger/escalation notifications all continue. A dangerous command on a paused
  armed tab STILL fires its NOTIFY (with the danger sound) - pause stops relay
  acting, never stops it warning you.
- **What a withheld approval looks like:** a would-be-approved safe prompt on a
  paused tab is simply not injected; the tab shows its normal `prompting`
  state and the prompt waits. No per-prompt audit row, no sound (that would
  spam every poll) - the global PAUSED banner is the single explanation.
- **Audited:** the pause and resume transitions each write one audit row
  (`paused` / `resumed`), so the timeline shows when relay's hands were tied.

### Loud indicator (the anti-footgun)

A paused relay that looks armed is dangerous. So:

- **Subtitle banner:** a pulsing `⏸ PAUSED - relay is NOT acting` replaces/leads
  the normal status line while paused.
- **Mascot:** a new top-of-ladder `paused` state in `effective_mascot_state`
  that OUTRANKS everything (even alarmed/critical) - the creature visibly
  freezes (flat eyes, `⏸` beacon, "paused - not acting"), in a distinct color.
  You can never mistake paused for guarding. Reuses the Legible Relay barometer.

### Dry-run interaction

In `--dry-run` relay already never acts, so pause changes nothing functional;
the toggle and the loud indicator still work (honest: it shows paused).

## Feature 2 - Shadow-arm

### The arm model

- A new per-tab mode value: `"shadow"`, alongside `off/safe/wild/insane`.
- `SessionInfo.active` stays `mode in ("safe","wild","insane")` - **shadow is
  NOT active**, so it never injects and is excluded from arm-all and every
  acting path, exactly like `off`. But it IS polled and classified.
- **Toggle:** `s` on the selected tab sets it to `shadow` (from any mode);
  `s` again returns it to `off`. The Space cycle is unchanged
  (`off -> safe -> wild -> insane -> off`); from `shadow`, Space promotes to
  `safe` (enters the cycle - the natural "calibrated, now commit" step).
- **Own panel tab** is never armable to shadow either (same guard as the acting
  modes).
- **Persistence:** shadow persists across a relay restart like the other modes
  (the existing arm-level mirror to the DB includes it).

### Behavior in `_handle`

When `info.mode == "shadow"`, relay runs the **safe**-mode decision (classify
the command via `danger.sh`, same predicate `safe` uses) and records what it
WOULD do, but never acts:

- would-approve (a safe permission prompt) -> audit `would-approve`, set
  displayed state `cleared`. No inject.
- would-escalate (dangerous / unreadable / fail-safe) -> audit `would-escalate`,
  set displayed state `blocked`/`prompting`. **No sound, no notification** -
  nothing real happened; you are watching. It is recorded and shown, not
  alarmed.
- The existing `prompt_id` debounce still applies, so each distinct prompt is
  recorded once, not every poll.

This is the `--dry-run` logic scoped to one tab, using safe's approve predicate.
The distinct value over global `--dry-run`: other tabs keep acting for real.

### Surfacing (reuses Legible Relay)

- **Live-feed pane:** the WHY line reads `WOULD CLEAR: safe permission prompt -
  grep foo` / `WOULD ESCALATE: dangerous command - ...` for a shadow tab.
- **Title prefix / status-bar badge:** a distinct shadow glyph (a hollow/eye
  marker) so a shadow tab reads differently from off and from armed. Adds a
  `shadow` entry to `titles.py`'s prefix map and `statusbar.py`'s `MODE_CIRCLE`.
- **Mascot:** shadow tabs count toward neither the armed guard count nor the
  cleared tally (nothing was actually cleared); optionally a small "shadowing N"
  note is a nice-to-have, not required.

## Architecture / touch points

- `iterm/watcher.py`: `self.paused` + toggle; pause gate in `_handle` (skip
  inject) and `_deliver` (early return); shadow branch in `_handle`; audit
  pause/resume; keep the poll loop and notifications running while paused.
- `iterm/app.py`: `action_pause` (`p`), `action_shadow` (`s`); Space-cycle
  handling of `shadow -> safe`; the PAUSED subtitle banner; `paused` mascot
  state (top of `effective_mascot_state`); WHY-line "WOULD ..." wording for
  shadow.
- `iterm/config.py` / `db.py`: `shadow` accepted as a valid persisted mode.
- `iterm/titles.py`, `iterm/statusbar.py`: a `shadow` glyph.
- `iterm/audit.py`: new verdicts `paused`, `resumed`, `would-escalate` (record
  only; `audit.record` is verdict-agnostic, so this is just new strings).

## Testing

Pure/unit where it counts, matching the existing suites:

- **Pause gate:** a paused watcher does NOT inject on a would-approve prompt and
  does NOT deliver a queued message, but STILL classifies and STILL notifies on
  a dangerous command (eyes open). Resume restores acting. (`test_watcher.py`,
  driving `_handle`/`_deliver` with `paused` set.)
- **Shadow:** a shadow tab records `would-approve` on a safe prompt and
  `would-escalate` on a dangerous one, never sends `\r`, never calls
  `notify_mac`. Debounce: the same prompt records once. `active` is False for
  shadow. (`test_watcher.py`.)
- **Arm transitions:** `s` sets shadow / returns to off; Space from shadow ->
  safe; own tab never becomes shadow; shadow persists/restores. (`test_app.py`
  for the toggle logic where pure, `test_watcher.py` for set_mode guards.)
- **Mascot paused state:** `effective_mascot_state` returns `paused` above all
  other states when paused. (`test_app.py`.)
- **Glyphs:** `shadow` present in the titles prefix map and `MODE_CIRCLE`.

## Rollout / risk

- **Fail-safe:** pause and shadow both err toward NOT acting - the safe
  direction. A bug that leaves relay paused or shadowed withholds action (the
  human notices nothing happened); it can never cause an un-intended action.
- **The one real hazard** is the inverse - a paused relay the user forgot -
  addressed by the loud, un-missable PAUSED banner + frozen mascot, and by
  pause never persisting across restart.
- Backward compatible: `shadow` is additive to the mode enum; existing configs
  and DB rows are unaffected.

## Out of scope (sequenced follow-ups)

1. **Frictionless setup & settings** (Spec 3): `relay doctor --fix`, guided
   first-run, TUI config editor (with the sound picker).
2. **PR-watch skill** (standalone).
3. **Swarm exposure** (parked).
