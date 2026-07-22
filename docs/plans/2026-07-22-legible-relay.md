# Legible Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make relay's actions glanceable and trustworthy by surfacing data it already computes - decision reasons, a running approval tally, differentiated sounds, and a recap.

**Architecture:** Purely additive. A thin in-process layer on the `Watcher` (`_last_event` pulse + done-detection) plus presentation changes in the TUI (`app.py`), config (`config.py`), and a new pure `recap.py`. No change to the classify/act pipeline.

**Tech Stack:** Python 3 (stdlib only), Textual TUI, iTerm2 Python API. Tests are plain `python3 iterm/test_*.py` runners (no pytest); the whole suite runs via `./test/run.sh`.

**Spec:** `docs/specs/2026-07-22-legible-relay-design.md`

## Global Constraints

- **No em-dash** (U+2014) or `&mdash;` anywhere - source, strings, comments, docs, commit messages. Use ASCII hyphen `-`.
- **No `Co-Authored-By` trailer** in commit messages.
- **Tests:** no pytest. Each `iterm/test_*.py` has a `run()`/`go()` and a `__main__` runner; run one file with `python3 iterm/test_<name>.py`; run all with `./test/run.sh`.
- **Config is `@dataclass(frozen=True)`** - add fields, never mutate instances.
- **Backward-compatible config:** existing `[sounds] alert`/`done` keys keep working; new keys default when absent.
- **Best-effort in the poll loop:** anything called inside `Watcher.start()`'s tick loop must never raise out (wrap in `try/except`, log via `self._note`). The status-bar/mascot/sound layer must never break the watcher.
- **Calm posture:** safe approvals never play a sound or fire a notification.

---

### Task 1: Sound config keys

**Files:**
- Modify: `iterm/config.py` (Config dataclass ~L36-46; `load()` return ~L137-147)
- Test: `iterm/test_config.py`

**Interfaces:**
- Produces: `config.Config.danger_sound: str` (default `/System/Library/Sounds/Basso.aiff`), `config.Config.message_sound: str` (default `/System/Library/Sounds/Tink.aiff`), read from `[sounds] danger` / `[sounds] message`.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_config.py` inside `run()`, after the existing "missing file -> defaults" check:

```python
    # New sound keys: defaults present, and overridable.
    ok &= check("missing file -> new sound defaults",
                cfg.danger_sound.endswith("Basso.aiff")
                and cfg.message_sound.endswith("Tink.aiff"))
    p2 = _write("[sounds]\ndanger = /a/x.aiff\nmessage = /a/y.aiff\n")
    cfg2, _ = config.load(p2)
    ok &= check("sound keys read from file",
                cfg2.danger_sound == "/a/x.aiff"
                and cfg2.message_sound == "/a/y.aiff")
    ok &= check("unset new keys fall back to defaults, others still read",
                config.load(_write("[sounds]\ndanger = /a/z.aiff\n"))[0]
                .message_sound.endswith("Tink.aiff"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_config.py`
Expected: FAIL - `AttributeError: 'Config' object has no attribute 'danger_sound'`

- [ ] **Step 3: Add the fields and loader**

In `iterm/config.py`, add to the `Config` dataclass right after `done_sound`:

```python
    done_sound: str = "/System/Library/Sounds/Glass.aiff"
    danger_sound: str = "/System/Library/Sounds/Basso.aiff"
    message_sound: str = "/System/Library/Sounds/Tink.aiff"
```

In `load()`, add to the `return Config(...)` call right after the `done_sound=` line:

```python
        done_sound=cp.get("sounds", "done", fallback=d.done_sound).strip(),
        danger_sound=cp.get("sounds", "danger", fallback=d.danger_sound).strip(),
        message_sound=cp.get("sounds", "message",
                             fallback=d.message_sound).strip(),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 iterm/test_config.py`
Expected: PASS (ends `ALL PASS`)

- [ ] **Step 5: Commit**

```bash
git add iterm/config.py iterm/test_config.py
git commit -m "feat(config): add [sounds] danger and message keys"
```

---

### Task 2: Watcher spine - events, differentiated sounds, done detection

**Files:**
- Modify: `iterm/gates.py` (add a shared constant; use it in `classify`)
- Modify: `iterm/watcher.py` (`__init__` ~L127-176; NOTIFY site ~L415-421; approve site ~L455; `_check_escalations` ~L662-668; tick loop ~L211-214; new method)
- Test: `iterm/test_gates.py`, `iterm/test_watcher.py`

**Interfaces:**
- Consumes: `config.Config.danger_sound`, `config.Config.message_sound` (Task 1).
- Produces:
  - `gates.DANGEROUS_COMMAND: str == "dangerous command"`.
  - `watcher._notify_sound(reason, *, danger, alert) -> str` (module-level pure helper).
  - `Watcher.danger_sound`, `Watcher.message_sound` (str attributes).
  - `Watcher._last_event: Optional[Tuple[str, float]]` - `(kind, ts)`, kind in `{"danger", "done"}` (drives mascot reactions in Task 4).
  - `Watcher._check_completions() -> None`.

- [ ] **Step 1: Write the failing gates test**

Add to `iterm/test_gates.py` inside its `run()` (near other classify checks):

```python
    from gates import DANGEROUS_COMMAND
    ok &= check("DANGEROUS_COMMAND constant is the danger reason string",
                DANGEROUS_COMMAND == "dangerous command")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_gates.py`
Expected: FAIL - `ImportError: cannot import name 'DANGEROUS_COMMAND'`

- [ ] **Step 3: Add the constant and use it in gates**

In `iterm/gates.py`, add a module-level constant near the top (after the imports / before `classify`):

```python
# The one reason string that means a confirmed dangerous command (as opposed to
# a fail-safe "I could not verify"). Shared with the watcher so the danger sound
# and mascot flinch key off the same value.
DANGEROUS_COMMAND = "dangerous command"
```

Then in `classify`, replace the literal in the dangerous-command return:

```python
        return Decision(Action.NOTIFY, DANGEROUS_COMMAND, command=cmd,
```

(Only that one occurrence - the `"dangerous command"` reason. Leave the other reason strings untouched.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_gates.py`
Expected: PASS

- [ ] **Step 5: Write the failing watcher tests**

Add a new function to `iterm/test_watcher.py` (before the `if __name__` block):

```python
def legible_spine_tests():
    """Watcher emits differentiated sounds + a _last_event pulse, and detects
    task completions edge-triggered (silent on the first tick)."""
    from watcher import Watcher, _notify_sound
    from gates import DANGEROUS_COMMAND
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # Pure sound routing.
    chk("danger reason -> danger sound",
        _notify_sound(DANGEROUS_COMMAND, danger="D", alert="A") == "D")
    chk("other reason -> alert sound",
        _notify_sound("real question - hands off", danger="D", alert="A") == "A")

    # Escalations play the MESSAGE sound (was alert).
    captured = []
    real_notify = W.notify_mac
    real_undeliv = W.swarmdb.undelivered
    real_pings = W.swarm.escalation_pings
    try:
        row = {"id": 1, "kind": "escalation", "from_name": "w1",
               "to_name": "c", "body": "help"}
        W.notify_mac = lambda t, m, s: captured.append(s)
        W.swarmdb.undelivered = lambda conn: [row]
        W.swarm.escalation_pings = lambda msgs, seen: msgs
        w = Watcher(connection=None, dry_run=False)
        w._swarm_conn = lambda: None
        w._check_escalations()
        chk("escalation uses message_sound",
            captured and captured[0] == w.message_sound)
    finally:
        W.notify_mac = real_notify
        W.swarmdb.undelivered = real_undeliv
        W.swarm.escalation_pings = real_pings

    # Completions: seed silently on the first tick, fire on a NEW done id.
    fired = []
    real_notify2 = W.notify_mac
    real_list = W.swarmdb.list_tasks
    try:
        tasks = [{"id": 1, "state": "done"}]
        W.swarmdb.list_tasks = lambda conn: list(tasks)
        W.notify_mac = lambda t, m, s: fired.append(s)
        w2 = Watcher(connection=None, dry_run=False)
        w2._swarm_conn = lambda: None
        w2._check_completions()
        chk("first tick seeds, does NOT fire", fired == []
            and w2._last_event is None)
        tasks.append({"id": 2, "state": "done"})
        w2._check_completions()
        chk("new done id fires done event + chime",
            len(fired) == 1 and fired[0] == w2.done_sound
            and w2._last_event is not None and w2._last_event[0] == "done")
        w2._check_completions()
        chk("no new done -> no repeat fire", len(fired) == 1)
    finally:
        W.notify_mac = real_notify2
        W.swarmdb.list_tasks = real_list

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

And wire it into the runner at the bottom:

```python
if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = asyncio.run(deliver_tests())
    r3 = asyncio.run(title_tests())
    r4 = arm_request_tests()
    r5 = closed_tests()
    r6 = asyncio.run(own_tab_name_tests())
    r7 = escalation_ratelimit_tests()
    r8 = asyncio.run(statusbar_registration_tests())
    r9 = legible_spine_tests()
    sys.exit(0 if (r1 and r2 and r3 and r4 and r5 and r6 and r7 and r8 and r9)
             else 1)
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL - `ImportError: cannot import name '_notify_sound'`

- [ ] **Step 7: Implement the watcher changes**

In `iterm/watcher.py`:

(a) Extend the gates import:

```python
from gates import (classify, Action, Decision, reconstruct_lines,
                   detect_state, DANGEROUS_COMMAND)
```

(b) Add the pure helper at module level (near `notify_mac`, ~L105):

```python
def _notify_sound(reason, *, danger, alert):
    """The sound a NOTIFY should play: the danger tone for a confirmed
    dangerous command, else the general alert. Pure - no I/O."""
    return danger if reason == DANGEROUS_COMMAND else alert
```

(c) In `__init__`, add params next to the existing sound params:

```python
                 alert_sound=None,
                 done_sound=None,
                 danger_sound=None,
                 message_sound=None,
```

and store them next to `self.done_sound = ...`:

```python
        self.alert_sound = alert_sound or cfg.alert_sound
        self.done_sound = done_sound or cfg.done_sound
        self.danger_sound = danger_sound or cfg.danger_sound
        self.message_sound = message_sound or cfg.message_sound
```

(d) In the swarm-state block of `__init__` (near `self._gone_notified = set()`), add:

```python
        self._last_event = None            # (kind, ts): danger|done reaction pulse
        self._done_seen: set = set()       # task ids already seen 'done'
        self._done_seen_init = False       # first tick seeds without firing
```

(e) At the NOTIFY site in `_handle`, change the sound and set the danger pulse:

```python
            info.n_escalated += 1
            if decision.reason == DANGEROUS_COMMAND:
                self._last_event = ("danger", time.time())
            audit.record("escalated", info.title, decision.command or "",
                         decision.reason)
            self._note(f"NOTIFY {info.title}: {decision.reason}")
            notify_mac(f"Relay - {info.title}",
                       decision.reason + (f": {decision.command[:80]}"
                                          if decision.command else ""),
                       _notify_sound(decision.reason,
                                     danger=self.danger_sound,
                                     alert=self.alert_sound))
            return
```

(f) In `_check_escalations`, change both `notify_mac(...)` calls' final argument from `self.alert_sound` to `self.message_sound`.

(g) Add the completion checker method (near `_check_escalations`):

```python
    def _check_completions(self) -> None:
        """Fire a 'done' pulse + chime the first time a task/epic reaches done.
        Seeds silently on the first tick so a pre-existing backlog does not
        chime on startup. Best-effort; never raises into the poll loop."""
        try:
            tasks = swarmdb.list_tasks(self._swarm_conn())
        except Exception:
            return
        done_ids = {t["id"] for t in tasks if t["state"] == "done"}
        if self._done_seen_init:
            new_done = done_ids - self._done_seen
            if new_done:
                self._last_event = ("done", time.time())
                notify_mac("Relay - done",
                           f"{len(new_done)} task(s) completed",
                           self.done_sound)
        self._done_seen = done_ids
        self._done_seen_init = True
```

(h) Call it in the tick loop right after `self._check_escalations()`:

```python
                self._check_escalations()
                self._check_completions()
```

(i) At the approve-success site, after `info.n_approved += 1`, no event is set (approvals are silent/ambient per the calm posture; the tally comes from summing `n_approved`). Leave that line as-is.

- [ ] **Step 8: Run to verify it passes**

Run: `python3 iterm/test_watcher.py`
Expected: PASS (all suites end `ALL PASS`)

- [ ] **Step 9: Commit**

```bash
git add iterm/gates.py iterm/watcher.py iterm/test_gates.py iterm/test_watcher.py
git commit -m "feat(watcher): differentiated sounds + done detection + reaction pulse"
```

---

### Task 3: Inline "why" in the live-feed pane

**Files:**
- Modify: `iterm/app.py` (add a pure helper; use it in `_update_preview` header ~L858-863)
- Test: `iterm/test_app.py`

**Interfaces:**
- Produces: `app.why_line(last_decision: str, last_command: str, width: int) -> str` - the ` WHY: <reason>: <cmd>\n` feed line, or `""` when there is no decision. Width-clamped.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_app.py` inside its `run()`:

```python
    from app import why_line
    ok &= check("why_line shows reason + command",
                why_line("safe permission prompt", "grep foo", 80)
                == " WHY: safe permission prompt: grep foo\n")
    ok &= check("why_line empty when no decision",
                why_line("", "grep foo", 80) == "")
    ok &= check("why_line reason only when no command",
                why_line("dangerous command", "", 80)
                == " WHY: dangerous command\n")
    ok &= check("why_line clamps to width",
                len(why_line("x" * 200, "y" * 200, 40)) <= 40)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL - `ImportError: cannot import name 'why_line'`

- [ ] **Step 3: Implement the helper and use it**

In `iterm/app.py`, add the pure helper near the other pure panel helpers (e.g. just above `getting_started_panel`):

```python
def why_line(last_decision: str, last_command: str, width: int) -> str:
    """The ' WHY: <reason>[: <cmd>]' live-feed line for the last decision relay
    made on a session, or '' when there is nothing to show. Pure, width-clamped
    (plain text - the pane renders literally)."""
    if not last_decision:
        return ""
    text = last_decision + (f": {last_command}" if last_command else "")
    return f" WHY: {text}"[:max(6, width)] + "\n"
```

Then in `_update_preview`, insert the line into the `header` (after the `MODE:` line, before `{attn}`):

```python
        why = why_line(info.last_decision, info.last_command, w)
        header = (f"╔{bar}╗\n"
                  f" ▓ LIVE FEED // {info.title[:w-16]}\n"
                  f" MODE:{mode}  LINK:{loc}  "
                  f"CLEARED:{info.n_approved}  HELD:{info.n_escalated}\n"
                  f"{why}"
                  f"{attn}"
                  f"╚{bar}╝\n")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_app.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): surface the last decision reason in the live-feed pane"
```

---

### Task 4: Mascot barometer - substance + earned reactions

**Files:**
- Modify: `iterm/app.py` (`mascot_state` area: add `effective_mascot_state`; guard/tally phrases; `mascot_face_big` ~L231-290; `banner_with_face` ~L300-315; `_tick_reactor` render call ~L813-820)
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `Watcher._last_event` (Task 2).
- Produces:
  - `app.effective_mascot_state(band, *, awaiting, working, armed, reaction=None) -> str` - returns `"flinch"`/`"celebrate"` for a live reaction (with alarmed/critical winning over `"done"`), else the base `mascot_state` value.
  - `app.mascot_face_big(tick, band, *, awaiting=0, working=False, armed=0, approvals=0, reaction=None) -> list`.
  - `app.banner_with_face(tick, band, *, awaiting=0, working=False, armed=0, approvals=0, reaction=None) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_app.py` inside `run()`:

```python
    from app import mascot_face_big, effective_mascot_state

    def joined(**kw):
        return " ".join(mascot_face_big(0, kw.pop("band", "ok"), **kw))

    ok &= check("guarding shows the cleared tally",
                "12" in joined(armed=3, approvals=12))
    ok &= check("guarding tally absent when zero approvals",
                "cleared" not in joined(armed=3, approvals=0))
    ok &= check("working shows the tally",
                "12" in joined(armed=3, working=True, approvals=12))
    ok &= check("done reaction renders celebration",
                "done" in joined(armed=3, approvals=5, reaction="done")
                and "★" in joined(armed=3, approvals=5, reaction="done"))
    ok &= check("danger reaction renders flinch",
                "danger" in joined(armed=3, reaction="danger")
                and "!" in joined(armed=3, reaction="danger"))
    # Precedence: a pending human need outranks a 'done' celebration.
    ok &= check("done does not override alarmed",
                effective_mascot_state("ok", awaiting=1, working=False,
                                       armed=1, reaction="done") == "alarmed")
    ok &= check("danger reaction wins as flinch",
                effective_mascot_state("ok", awaiting=0, working=False,
                                       armed=1, reaction="danger") == "flinch")
    ok &= check("no reaction -> base state",
                effective_mascot_state("ok", awaiting=0, working=False,
                                       armed=2, reaction=None) == "guarding")
    ok &= check("face is always 6 lines",
                len(mascot_face_big(0, "ok", armed=3, reaction="done")) == 6)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL - `ImportError: cannot import name 'effective_mascot_state'`

- [ ] **Step 3: Add effective_mascot_state and the tally phrases**

In `iterm/app.py`, add right after `mascot_state`:

```python
def effective_mascot_state(band, *, awaiting, working, armed, reaction=None):
    """The state that drives the frame + color, folding in a momentary reaction.
    A 'danger' reaction is its own flinch (it IS the alarm). A 'done' reaction
    celebrates - unless a human is already needed (alarmed) or the core is
    critical, which outrank a celebration. No reaction -> the base ladder."""
    if reaction == "danger":
        return "flinch"
    base = mascot_state(band, alarmed=awaiting > 0, working=working, armed=armed)
    if reaction == "done" and base not in ("alarmed", "critical"):
        return "celebrate"
    return base
```

Add a tally-aware guard phrase set next to `MASCOT_GUARD_PHRASES`:

```python
# When relay has cleared work this run, the guard lines report substance -
# "N cleared" is the walk-away-trust sentence. {n}=armed, {a}=approvals.
MASCOT_GUARD_TALLY_PHRASES = (
    "{a} cleared, quiet.", "guarding {n} · {a} done.",
    "eyes on {n} · {a} cleared.", "nothing needs you ({a} done).",
)
```

- [ ] **Step 4: Thread approvals + reaction through mascot_face_big**

Change the signature:

```python
def mascot_face_big(tick: int, band: str, *, awaiting: int = 0,
                    working: bool = False, armed: int = 0,
                    approvals: int = 0, reaction=None) -> list:
```

Replace the `state = mascot_state(...)` line inside it with (note: `awaiting` is an int; `effective_mascot_state` converts it to `alarmed=awaiting > 0` internally):

```python
    state = effective_mascot_state(band, awaiting=awaiting, working=working,
                                   armed=armed, reaction=reaction)
```

Add the two reaction branches at the TOP of the `if/elif` chain (before `if state == "alarmed"`):

```python
    if state == "celebrate":
        eyes, mid, mouth, beacon = " ^  ^ ", "   ✓  ", "  ◡   ", "★"
        say = "task done ★"
    elif state == "flinch":
        eyes, mid, mouth, beacon = " O  O ", "   !  ", "  □   ", "!"
        say = "whoa - danger"
    elif state == "alarmed":
```

In the `working` branch, append the tally to the verb line when there is one:

```python
        verb = MASCOT_WORKING_PHRASES[tick // 16 % len(MASCOT_WORKING_PHRASES)]
        say = verb + ("." * (tick % 4))
        if approvals:
            say = f"{verb} · {approvals}"
```

In the `guarding` branch, choose the tally phrases when approvals > 0:

```python
        beacon = "⌖"
        if approvals:
            phrases = MASCOT_GUARD_TALLY_PHRASES
            say = phrases[tick // 48 % len(phrases)].format(n=armed, a=approvals)
        else:
            say = MASCOT_GUARD_PHRASES[
                tick // 48 % len(MASCOT_GUARD_PHRASES)].format(n=armed)
```

- [ ] **Step 5: Extend the color map and banner_with_face**

Add the two reaction colors to `_MASCOT_COLOR`:

```python
_MASCOT_COLOR = {"alarmed": WARN, "critical": DANGER,
                 "working": BRIGHT, "guarding": ACCENT, "idle": DIM,
                 "celebrate": BRIGHT, "flinch": WARN}
```

Change `banner_with_face` to accept and forward the new args:

```python
def banner_with_face(tick: int, band: str, *, awaiting: int = 0,
                     working: bool = False, armed: int = 0,
                     approvals: int = 0, reaction=None) -> str:
```

Replace its `state = mascot_state(...)` line with:

```python
    state = effective_mascot_state(band, awaiting=awaiting, working=working,
                                   armed=armed, reaction=reaction)
    color = _MASCOT_COLOR[state]
```

and forward to the face:

```python
    face = mascot_face_big(tick, band, awaiting=awaiting, working=working,
                           armed=armed, approvals=approvals, reaction=reaction)
```

- [ ] **Step 6: Wire the watcher's tally + pulse into the render**

In `_tick_reactor`, just before the `self.query_one("#banner", ...)` update, compute the reaction from the watcher's pulse and the tally:

```python
        REACTION_TTL = 1.5
        reaction = None
        ev = getattr(self.watcher, "_last_event", None)
        if ev and (time.time() - ev[1]) <= REACTION_TTL:
            reaction = ev[0]
        approvals = sum(i.n_approved for i in self.watcher.sessions.values()
                        if i.session_id != self._own_sid)
```

Then extend the banner update call:

```python
            self.query_one("#banner", Static).update(banner_with_face(
                self._tick, label, awaiting=awaiting,
                working=self._tick < getattr(self, "_mascot_active_until", 0),
                armed=sum(1 for i in self.watcher.sessions.values()
                          if i.active and i.session_id != self._own_sid),
                approvals=approvals, reaction=reaction))
```

Confirm `time` is imported at the top of `app.py` (it is used elsewhere; if not, add `import time`).

- [ ] **Step 7: Run to verify it passes**

Run: `python3 iterm/test_app.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): mascot barometer - cleared tally + done/danger reactions"
```

---

### Task 5: `relay recap` (module + CLI + on-quit line)

**Files:**
- Create: `iterm/recap.py`
- Create: `iterm/test_recap.py`
- Modify: `iterm/cli.py` (add `cmd_recap`; register subparser in `build_parser`)
- Modify: `iterm/app.py` (`main()` ~L1228, print a recap line after the panel closes)
- Modify: `test/run.sh` - no change needed (globs `test_*.py` automatically)

**Interfaces:**
- Produces:
  - `recap.summarize(entries: list, since: float) -> dict` with keys `cleared`, `woke`, `delivered` (ints).
  - `recap.start_of_today() -> float` - local-midnight epoch.
  - `cli.cmd_recap(args) -> int`.

- [ ] **Step 1: Write the failing recap test**

Create `iterm/test_recap.py`:

```python
"""Tests for pure recap aggregation. No iTerm2, no file I/O.

Run: python3 iterm/test_recap.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import recap  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    entries = [
        {"ts": 100.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "escalated"},
        {"ts": 200.0, "verdict": "delivered"},
        {"ts": 50.0,  "verdict": "auto-approved"},   # before window
        {"ts": 160.0, "verdict": "would-approve"},   # dry-run, not counted
        {"bogus": 1},                                # garbled, skipped
    ]
    s = recap.summarize(entries, since=100.0)
    ok &= check("cleared counts auto-approved in window", s["cleared"] == 2)
    ok &= check("woke counts escalated", s["woke"] == 1)
    ok &= check("delivered counts delivered", s["delivered"] == 1)

    empty = recap.summarize([], since=0.0)
    ok &= check("empty log -> zeros",
                empty == {"cleared": 0, "woke": 0, "delivered": 0})
    ok &= check("start_of_today is a float epoch",
                isinstance(recap.start_of_today(), float)
                and recap.start_of_today() > 0)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_recap.py`
Expected: FAIL - `ModuleNotFoundError: No module named 'recap'`

- [ ] **Step 3: Create the recap module**

Create `iterm/recap.py`:

```python
"""Pure recap aggregation over audit entries. No I/O, no iTerm2 - the CLI
(relay recap) and the on-quit line both read the audit log and hand the rows
here. Mirrors the statusbar.py split: pure logic here, I/O at the call site."""
import time


def start_of_today() -> float:
    """Local-midnight epoch seconds - the default recap window start."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def summarize(entries, since: float) -> dict:
    """Count audit verdicts at/after `since`. Returns the headline tallies.
    Never raises on odd or partial rows."""
    out = {"cleared": 0, "woke": 0, "delivered": 0}
    for e in entries:
        try:
            if float(e.get("ts", 0)) < since:
                continue
            v = e.get("verdict")
        except Exception:
            continue
        if v == "auto-approved":
            out["cleared"] += 1
        elif v == "escalated":
            out["woke"] += 1
        elif v == "delivered":
            out["delivered"] += 1
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_recap.py`
Expected: PASS

- [ ] **Step 5: Add the CLI command**

In `iterm/cli.py`, add `cmd_recap` (near `cmd_doctor`):

```python
def cmd_recap(args) -> int:
    """Summarize what relay did (reads the audit log + task board; never
    mutates). Default window: today; --all for all time."""
    import audit
    import recap
    since = 0.0 if getattr(args, "all", False) else recap.start_of_today()
    s = recap.summarize(audit.read_tail(limit=100000), since)
    conn = db.connect()
    from collections import Counter
    by = Counter(t["state"] for t in db.list_tasks(conn))
    window = "all time" if since == 0.0 else "today"
    print(f"relay recap ({window})")
    print(f"  cleared {s['cleared']} · woke you {s['woke']}x · "
          f"delivered {s['delivered']}")
    print(f"  tasks: {by['done']} done · {by['doing']} doing · "
          f"{by['blocked']} blocked · {by['todo']} todo")
    return 0
```

Register the subparser in `build_parser` (next to the `doctor` parser):

```python
    rp = sub.add_parser("recap",
                        help="summarize what relay did today (reads audit log)")
    rp.add_argument("--all", action="store_true",
                    help="all time, not just today")
    rp.set_defaults(fn=cmd_recap)
```

- [ ] **Step 6: Add a test for the CLI wiring**

Add to `iterm/test_cli.py` inside its `run()` (follow the file's existing parser-check idiom; this asserts the subcommand parses and dispatches):

```python
    import cli
    args = cli.build_parser().parse_args(["recap", "--all"])
    ok &= check("recap subcommand dispatches to cmd_recap",
                args.fn is cli.cmd_recap and args.all is True)
    args2 = cli.build_parser().parse_args(["recap"])
    ok &= check("recap defaults to today (all=False)", args2.all is False)
```

- [ ] **Step 7: Run the CLI test**

Run: `python3 iterm/test_cli.py`
Expected: PASS

- [ ] **Step 8: Print a recap line on panel quit**

In `iterm/app.py`, in `main()`, replace the final launch line:

```python
    RelayApp(dry_run=dry).run(mouse=False)
```

with:

```python
    RelayApp(dry_run=dry).run(mouse=False)
    # Legible Relay: a one-line recap once the panel closes (best-effort).
    try:
        import audit
        import recap
        s = recap.summarize(audit.read_tail(limit=100000),
                            recap.start_of_today())
        print(f"relay: today - cleared {s['cleared']} · "
              f"woke you {s['woke']}x · delivered {s['delivered']}")
    except Exception:
        pass
```

- [ ] **Step 9: Run the full suite**

Run: `./test/run.sh`
Expected: ends `ALL SUITES PASSED`

- [ ] **Step 10: Manual smoke check**

Run: `python3 iterm/cli.py recap`
Expected: prints `relay recap (today)` with the tally lines (zeros on a fresh log - no crash).

- [ ] **Step 11: Commit**

```bash
git add iterm/recap.py iterm/test_recap.py iterm/cli.py iterm/app.py iterm/test_cli.py
git commit -m "feat(cli): relay recap + one-line summary on panel quit"
```

---

## Self-Review

**1. Spec coverage:**
- The shared spine (`_last_event`, tally via summed `n_approved`, done detection) - Task 2 + consumed in Task 4. ✓
- #1 inline-why - Task 3. ✓
- #4 mascot barometer (substance + done/danger reactions, calm on approvals) - Task 4. ✓
- #5 sounds as config (4 keys, routing, revived `done`) - Task 1 (keys) + Task 2 (routing/danger/message/done). ✓
- #6 recap (CLI + on-quit) - Task 5. ✓
- Testing in pure iTerm2-free helpers - every task tests a pure helper. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code; every test step shows assertions. ✓

**3. Type consistency:** `_notify_sound`, `effective_mascot_state`, `mascot_face_big`, `banner_with_face`, `recap.summarize`, `recap.start_of_today`, `why_line`, `cmd_recap` names/signatures match across the tasks that define and consume them. `_last_event` is `(kind, ts)` set in Task 2 and read in Task 4. ✓

**Note on the danger-reaction pulse:** the `_last_event = ("danger", ...)` assignment (Task 2, step 7e) is guarded by the same `decision.reason == DANGEROUS_COMMAND` predicate that `_notify_sound` is unit-tested against; its integration is exercised when a dangerous prompt flows through `_handle`.
