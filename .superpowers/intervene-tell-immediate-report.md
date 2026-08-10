# INTERVENE: TELL sends immediately, not queued - implementation report

## Summary

TELL now types the operator's message straight into the target tab and
submits it, via the same `send_keys(sid, text)` mechanism STOP already uses
for its ESC. Nothing is written to the swarm message queue on this path, and
nothing waits for the target to reach an idle prompt. This fixes two real
failures reported by the operator: TELL doing nothing at all for unregistered
tabs (the normal case on their machine - zero registered sessions), and
STOP + TELL appearing to do nothing because the message half silently waited.

## What changed, file:line

### `iterm/app.py`

- `_INTERVENE_ROWS` (line 704-708): mode-row timing labels changed from
  `"ESC now · on idle"` / `"text on idle"` to `"ESC now · text now"` /
  `"text now"` - the modal's own timing-honesty row was still claiming TELL
  waited for idle; leaving it would have contradicted the fix in the same
  screen the operator reads before pressing ENTER. (Not explicitly listed in
  the task's "what to change", but directly follows from it - flagged as a
  judgment call in Concerns below.)
- `intervene_modal_text` (line 711-751): dropped the `n_tellable` parameter
  and the `"{n_tellable} tellable"` suffix on the count line; rewrote the
  docstring to describe the immediate-send mechanism instead of the old
  ESC-now/message-on-idle split.
- `_intervene_render` (line 2984-2991): dropped the
  `swarmlogic.intervene_tellable_count(targets)` call and the now-removed
  argument to `intervene_modal_text`.
- `_intervene_execute` (line 3028-3037): docstring rewritten - both halves
  are immediate now, no more "TELL only queues" language.
- TELL send loop (line 3079-3095): replaced the `queue_message` loop
  (guarded by `if not t["name"]: continue`) with a `send_keys` loop over
  every target unconditionally - `swarmlogic.delivery_text("human", body,
  kind="info")` builds the sanitised, labelled text once, then each target
  gets `send_keys(sid, text)` followed by a separate `send_keys(sid, "\r")`,
  both dispatched via `self.run_worker(..., exclusive=False)` exactly as
  STOP's ESC already is.
- Report wording (line 3103-3110): `queue_failed` tracking removed (no
  queue write to fail); the TELL report line changed from
  `"queued N (delivered on next idle prompt)"` (plus a paused-relay caveat)
  to `"told N"`.

### `iterm/swarm.py`

- Removed `intervene_tellable_count` (was line 486-491) - it becomes dead
  code once `n_tellable` is gone from `intervene_modal_text`'s signature and
  nothing else called it.

### Tests

- `iterm/test_swarm.py`: removed the `intervene_tellable_count` import and
  its three unit-test assertions.
- `iterm/test_extreme.py` (`test_intervene_modal_text`): every
  `intervene_modal_text` call site dropped its `n_tellable` argument;
  "timing shown per mode" now checks for `"text now"` instead of
  `"on idle"`; "tellable count shown" replaced with a check that no
  `"tellable"` string renders at all; removed the now-meaningless "STOP mode
  does not show a tellable count" check.
- `iterm/test_app.py`: reworked the TELL-related blocks in the main
  intervene pilot-driven test, plus the "finding 3" (mixed registration) and
  "finding 4" (queue-write failure) blocks, and added assertions to the
  "finding 5" (audit) block for STOP + TELL. Details below.

## The exact bytes sent for a TELL

For buffer text `"hi"`, sent with `kind="info"` and `from_name="human"`:

1. `send_keys(sid, "[relay msg from human] hi")`
2. `send_keys(sid, "\r")`

(`swarm.delivery_text` maps `kind="info"` to the tag `"msg"` - `tag = "msg"
if kind in ("", "info") else kind` - so the label reads `[relay msg from
human]`, not `[relay info from human]`. I initially wrote the wrong label in
my first test draft and the test failure caught it immediately; see revert
evidence below, which was run against the corrected version.)

STOP + TELL on a working target sends, in order: `\x1b` (STOP's ESC, no
trailing return), then the two TELL sends above. An idle target in the same
STOP + TELL call gets only the two TELL sends - the ESC is skipped for idle
targets exactly as it always was, but TELL is not gated on working state, so
the message still lands.

## The report modal's new wording, per mode

- STOP: `interrupted {n} · skipped {n} (idle)` - unchanged.
- TELL: `told {n}` - replaces `queued {n} (delivered on next idle prompt)`
  and the `- relay PAUSED, held until resumed` suffix (both removed
  entirely; TELL never goes through relay's pause gate, since it is a
  direct human-initiated send like STOP and the manual 1/2/3/ENTER keys).
- STOP + TELL: both lines together, e.g.:
  ```
  interrupted 1 · skipped 1 (idle)
  told 2
  extreme disarmed on 1
  ```
- The `queue write FAILED - some messages may be missing` line is gone
  entirely - there is no queue write left to fail on this path.

## Per-assertion revert evidence

Reverted `iterm/app.py` and `iterm/swarm.py` only (via `git stash push -- 
iterm/app.py iterm/swarm.py`), kept the new/updated tests, and re-ran:

- `iterm/test_app.py`:
  - `FAIL TELL sends keystrokes to every target in scope (3 targets x text +
    return)` - fails against the old queue-based implementation (it sent
    nothing for unregistered targets).
  - `FAIL TELL against an unregistered target actually sends` - fails; the
    old code then crashes on the very next line
    (`s2_sends[0][1]`, `IndexError: list index out of range`) because
    `s2_sends` is empty - the old TELL sent zero keystrokes to the
    unregistered target `s2`. This is the exact bug the operator hit.
  - Because of that crash, later checks in the same run (the sanitised-text
    check, the return check, STOP + TELL's two new checks, the
    `queue_message`-poisoned check) never even executed under the reverted
    code - the crash itself is stronger evidence than a clean FAIL would
    have been.
- `iterm/test_extreme.py`: crashes immediately with `TypeError:
  intervene_modal_text() missing 1 required positional argument: 'width'`
  - the reverted `intervene_modal_text` still requires the old `n_tellable`
    positional parameter that the updated test call sites no longer pass.
    Every assertion in `test_intervene_modal_text` is therefore unreachable
    under the old signature.
- `iterm/test_swarm.py`: passed unchanged (that suite's intervene tests
  don't touch `n_tellable`/`intervene_tellable_count` after my edits removed
  those references from it).

Restored the fix with `git stash pop`, re-ran the full suite: `ALL SUITES
PASSED` (see below).

Individual isolation: each of the two headline `test_app.py` assertions was
confirmed to independently require the fix - "sends keystrokes" fails
because `a.watcher.sent` stays empty for the whole TELL-only block (queue
path wrote to sqlite, not to the stub's `.sent` list), and "against an
unregistered target actually sends" fails for the same reason on a second,
separately-cleared `.sent` list. They are not redundant with each other (one
exercises a mixed idle/working scope, the other isolates the unregistered
target specifically), and both were necessary+sufficient: neither passes
under the old code, both pass under the new code.

## Spec changes (`docs/specs/2026-08-09-intervene-design.md`)

Quoted, in order of appearance:

**"The load-bearing rule: timing honesty"** section - rewritten from "An
interrupt is immediate. A message is delivered when the session is next idle
at a ready prompt" to:

> Both halves are **immediate**. STOP sends a bare ESC now; TELL types the
> message straight into the tab and submits it now - the same
> `send_keys(sid, text)` mechanism, on a session id, that STOP's ESC and
> relay's manual `1`/`2`/`3`/`ENTER` sends already use. Neither goes through
> a queue, and neither waits for the target to reach a ready prompt.
>
> This was not always true. TELL originally queued an ordinary swarm message
> addressed to the target's registered name, delivered later by the watcher
> on that session's next idle poll (see the decisions log). That had two
> failures: it did nothing at all for a tab that never ran `relay join` (no
> name, no mailbox, `queued 0`), and on a normal operator machine with zero
> registered sessions that made TELL entirely inert. It also meant an
> operator who pressed STOP + TELL expecting the message to arrive watched
> nothing happen. Both are why TELL now sends immediately, straight to the
> session id, like everything else in this modal.

Section 1's modal mockup: `ESC now · on idle` -> `ESC now · text now`,
`text on idle` -> `text now`.

Section 2's table row: `TELL | a queued message | on idle | refused` ->
`TELL | typed text + \r | now | refused` (and the STOP + TELL row's timing
column to match). The `"TELL queues an ordinary message..."` paragraph
replaced with:

> **TELL types the message straight into the tab with `send_keys(sid,
> text)`, then submits it with a separate `send_keys(sid, "\r")`.** This is
> the same mechanism STOP's ESC and the manual `1`/`2`/`3`/`ENTER` sends
> use, keyed on the session id rather than a swarm name, so it reaches a
> target whether or not it is registered. The text is passed through
> `swarm.delivery_text(from_name="human", body, kind="info")` first - the
> same sanitiser the queued-message path used, still labelling the sender
> `human` ... There is no queue and no batching on this path - each TELL is
> one send per target, right now.
>
> STOP + TELL fires both sends for a target in the same pass: the ESC first
> (when the target is working), then the message and its return. There is
> no ordering gap to reason about...

Section 6's report block and paragraph updated to `told 3` instead of
`queued 3 (delivered on next idle prompt)`, and the "it also covers partial
failure honestly: if the interrupt lands and the queue write fails..."
paragraph removed (no queue write exists to fail).

Section 8 (Testing) got a new bullet documenting the unregistered-TELL
regression test and the `queue_message`-poisoned test.

Accepted costs got a new line: `"Nothing confirms a TELL was actually
received."` (matching the existing STOP entry's honesty about
fire-and-forget sends).

Decisions log got a new row:

> | TELL sends via `send_keys(sid, text)` immediately, not
> `db.queue_message` (amendment) | Keeping the queued delivery, addressed to
> the target's swarm name | The queue path silently did nothing for an
> unregistered tab (`messages.to_name` needs a name; no name, no mailbox,
> `queued 0`) - on a normal operator machine with zero registered sessions,
> that made TELL entirely inert. It also meant STOP + TELL's message arrived
> only after the target's next idle poll, not "now", which an operator
> watching for it experienced as the feature doing nothing. `send_keys` is
> keyed on the session id relay already has for every tab, registered or
> not, and is the same fire-and-forget mechanism STOP's ESC and the manual
> sends already use - one less delivery path for this one to disagree
> with. |

## README changes

Replaced:

> - **TELL** queues a message for each targeted session that is registered,
>   delivered the next time that session reaches a ready prompt. It does not
>   interrupt. An unregistered tab has no mailbox to queue into, so it is
>   not told.
> - **STOP + TELL** does both, each with its own targeting above: the `ESC`
>   lands now, the message waits for idle.
>
> The timing is not interchangeable: typing "stop, do X instead" as TELL on
> a working session means X arrives only once that session finishes the turn
> you wanted it to abandon.

with:

> - **TELL** types the message straight into every targeted session and
>   hits Enter, right away - the same mechanism `STOP`'s `ESC` uses, and the
>   same one behind relay's manual `1`/`2`/`3`/`ENTER` sends. It works on
>   any tab, registered or not: it's keyed on the session, not a swarm name.
>   It does not interrupt - a working session just gets the text queued at
>   its own input, same as if you'd typed it yourself.
> - **STOP + TELL** does both, in the same pass: the `ESC` first (on
>   working targets), then the message and its return.
>
> Nothing here is queued, and nothing waits for a session to go idle - both
> modes act the moment you press `ENTER` on the modal.

The rest of the section (extreme-disarm, timers note) needed no changes -
neither referenced the queue.

## Test suite output

`RELAY_DB=<tmp>/relay.db ./test/run.sh` -> `ALL SUITES PASSED` (all
`test_*.py` suites plus the bash classifier suite). Full log saved at
`/private/tmp/claude-502/-Users-maciej-Work-relay/0f8fb560-d6cb-4b58-b57a-37da85084d2d/scratchpad/test_run_final.txt`.

## Concerns

- I changed `_INTERVENE_ROWS`'s on-screen timing labels (`"ESC now · on
  idle"` -> `"ESC now · text now"`, `"text on idle"` -> `"text now"`) even
  though the task's numbered "what to change" list didn't call this row out
  explicitly - it only named the report wording and `n_tellable`. I made
  this call because leaving the composing modal claiming TELL waits "on
  idle" would have directly contradicted the fix's whole point (the spec's
  own "timing honesty" rule) in the same screen the operator reads right
  before pressing ENTER. Flagging in case this was meant to stay
  untouched - it's easy to revert if so.
- TELL's `run_worker(..., exclusive=False)` calls are fire-and-forget, same
  as STOP's ESC - the `told` count in the report is sends issued, not sends
  confirmed. This mirrors STOP's existing behavior and is called out in the
  code comment and the spec's accepted-costs list, not something I consider
  a defect, but worth knowing: a `send_keys` failure (e.g. a closed pane
  mid-call) would not be reflected in the count.
- I did not touch `watcher.py`'s `_deliver` or the swarm message queue used
  by `relay send`/`relay ask`/discussions - confirmed by grep that
  `queue_message` no longer appears anywhere in `iterm/app.py`.
