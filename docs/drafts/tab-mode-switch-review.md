# Review: Tab-Side Mode Switching Design

**Reviewer:** critic
**Date:** 2026-07-15
**Design:** docs/drafts/tab-mode-switch-design.md
**Verdict:** Sound core decision (RPC channel over DB row), one major gap the
design explicitly declines to close, one underspecified fix, several
unverified API assumptions. Not ready to plan until concerns 1 and 2 are
resolved in the doc.

All code claims below were verified against the working tree at review time.

## What the design gets right

- **Rejecting the `mode_request` DB row (6.1) is correct.** The DB is
  world-writable local state with no provenance; the IDEAS.md sketch would
  have institutionalized a self-arming channel. Killing it outright, not
  deferring it, is the right call.
- **The 3.3 discovery is real and verified.** `watcher.py:417-421` applies
  `arm_request` to any registered session present in `self.sessions`, every
  tick the column is non-empty. Nothing restricts it to fresh spawns. The
  sqlite3-self-escalation scenario is accurate: `lib/danger.sh` has no
  pattern matching a `sqlite3 ... UPDATE` and a safe-armed session would get
  it auto-approved. Surfacing this as a prerequisite is the most valuable
  paragraph in the doc.
- **Display integrity (3.4) is the right architecture.** Answering the
  status bar callback from `SessionInfo.mode` in-process, never from user
  variables, makes the display exactly as trustworthy as the watcher.
- **Lifecycle story (2.3) is clean.** No daemon, registrations die with the
  connection, honest failure when relay is closed.
- **Single state holder (1.4).** No reconciliation problem because nothing
  new holds state. Good.

## Concern 1 (MAJOR): `relay spawn --arm` is self-arming by proxy, and the design keeps it

Section 3.1 states the invariant: a process inside a session must not be
able to grant itself approval powers. Section 3.3 then bans a `relay arm`
CLI verb ("self-arming as a feature") while explicitly keeping
`relay spawn --arm` working unchanged.

These two positions are inconsistent. The relay CLI is on every worker's
PATH (`spawn.py` exports it into the worker shell). A session armed `safe`
can run:

    relay spawn --arm insane --name pwn "any prompt the session chooses"

`relay spawn` matches nothing in `lib/danger.sh`, so it classifies safe and
auto-approves. The result is a brand-new insane-armed session executing
attacker-chosen instructions. The session cannot arm ITSELF, but it can mint
an armed puppet - which is the same capability. The 3.3 fix closes direct
arming of existing sessions and leaves this proxy path fully open; even a
correct "never seen before" implementation is bypassed by it, because a
freshly spawned tab is legitimately never-seen.

The design must address this, not scope it out. Options, cheapest first:

- Add `relay spawn` (at minimum any invocation carrying `--arm`) to
  `danger.sh` dangerous patterns, so safe-armed sessions can never
  auto-approve it. Wild/insane sessions still can - state that residual risk
  explicitly if accepted.
- Fire the escalation sound + macOS notification whenever an `arm_request`
  is honored, not just a `_note` line (watcher.py:421 today). A human should
  hear every arming event that did not come from their own hand, tab-side
  changes and spawn pre-arms alike. The design gives tab-side escalations a
  sound but leaves DB-channel arming silent - backwards, given the DB
  channel is the less trustworthy one.
- Consider capping `--arm` from non-human contexts, though provenance is
  admittedly unknowable at the CLI layer.

## Concern 2 (MAJOR): the "never seen before" fix is underspecified and has a race

Section 3.3's fix says: honor `arm_request` only for sessions the watcher
has never seen; "the request is written before the tab exists, so it is
always never-seen when first applied." Two problems, verified against the
code:

1. **The claim about ordering is false as written.** `spawn.py` creates the
   tab FIRST (`async_create_tab`, line ~55), then registers and writes
   `arm_request` (lines 66-71). `_sync_sessions` enumerates ALL iTerm
   sessions every 2s tick and adds them to `self.sessions` immediately - it
   does not wait for registration. If a watcher tick lands in the window
   between tab creation and the DB write, the session is recorded as "seen
   with request absent", and the fix as specified would reject a legitimate
   spawn pre-arm, clear it, and fire a rejection notification. Small window,
   real race, fails in the direction that breaks the shipped `--arm`
   feature intermittently.
2. **"Seen" needs a precise key.** If keyed by session NAME (registry row),
   a session can re-register itself under a fresh name (`relay register` is
   on its PATH) and the new binding looks never-seen: self-arm succeeds and
   the fix is void. It must be keyed by iTerm session id. The doc doesn't
   say, and the difference is the whole fix.

Suggested tightening: key first-seen by iTerm session id; honor a request
only if it is present on the very first tick the sid enters
`self.sessions`, OR within a short grace window (1-2 ticks) of first sight
to absorb the race. And say plainly what remains true either way: the DB is
a local-trust channel, the fix narrows the window, it does not create
provenance.

## Concern 3: load-bearing iTerm2 API behaviors are asserted, never validated

The security argument and the UX both rest on specific iTerm2 behaviors
that the design states as fact but nothing in the testing strategy (8)
exercises:

- `iterm2.Reference("id")` resolving to the focused session when an RPC is
  invoked from an "Invoke Script Function" key binding.
- `StatusBarComponent` supporting a click handler that receives the session
  identity.
- The "unknown function" toast when relay is closed (this is the entire
  "honest failure" story of 2.3).
- RPC callbacks dispatching on the same asyncio loop as the watcher tick
  (the "no locking needed" claim of 2.2 - plausible given app.py:191 shares
  one loop, but asserted, not shown).
- "There is no escape sequence that invokes a script function" - the load-
  bearing sentence of 3.2, resting on iTerm2 internals across versions.

The pure-function tests in section 8 test none of these; the live checklist
is manual and post-implementation. Before this design is accepted, a
half-day spike should register a throwaway RPC + status bar component and
confirm each behavior on the actual iTerm2 version in use. Cheap, and it
de-risks the only part of the design that cannot be unit-tested.

## Minor issues

- **Two relay instances.** If two TUIs run (the swarm docs acknowledge
  multi-coordinator collisions), both watchers try to register the same RPC
  names. iTerm2's behavior on duplicate registration (error? silent
  replace? which process wins the key binding?) is not in the failure table
  (5). Add a row.
- **Audit asymmetry.** 3.5 audits tab-side changes but not TUI `Space`. The
  stated rationale (new trigger surface) is fine, but once an audit path
  for mode changes exists, auditing all three sources (TUI, tab-side,
  spawn pre-arm) costs one line each and makes the trail complete. Recommend
  auditing all mode changes, source-tagged.
- **`relay_disarm` semantics.** Straight-to-off is good; consider whether it
  should also be the documented panic key when the watcher is mid-injection
  (does disarm take effect before the next gate evaluation of an already
  approved prompt? The `_last_prompt_id` reset suggests yes - say so).
- **Escalation sound opt-out.** "Pick a silent alert sound globally" also
  silences approval escalation alerts, which users may want to keep. Minor,
  but the coupling is worth a sentence.
- **Status bar refresh latency.** iTerm2 status bar components poll on a
  configured cadence; a stale badge after a keybind change could briefly
  show the wrong mode. The title-prefix path has the same tick lag. Fine,
  but 1.3 should state the worst-case staleness so nobody files it as a bug.

## Summary for the coordinator

1. `relay spawn --arm` on worker PATH = self-arming by proxy; design bans a
   `relay arm` verb yet keeps this equivalent open. Must be addressed.
2. The prerequisite 3.3 fix is underspecified: needs sid-keyed first-seen
   plus a grace window, or spawn's tab-then-register ordering makes it
   reject legitimate pre-arms; name-keyed variant would be bypassable.
3. Five load-bearing iTerm2 API behaviors are asserted without validation;
   a half-day spike belongs in the plan before implementation.
