# Reversible Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two ways to stop or withhold relay's actions - a global pause and a per-tab shadow (dry-run) mode - both failing toward not-acting.

**Architecture:** Purely additive to the act path. A `paused` flag on the `Watcher` gates the inject and delivery; a new `shadow` mode records `would-*` verdicts without acting. TUI surfaces both (a loud PAUSED banner + frozen mascot state; a shadow glyph).

**Tech Stack:** Python 3 (stdlib only), Textual TUI, iTerm2 API. Tests are `python3 iterm/test_*.py`; whole suite via `./test/run.sh`.

**Spec:** `docs/specs/2026-07-22-reversible-relay-design.md`

## Global Constraints

- **No em-dash** (U+2014) or `&mdash;` anywhere - source, strings, comments, commit messages. ASCII hyphen `-` only.
- **No `Co-Authored-By` trailer** in commit messages.
- **Tests:** no pytest. `python3 iterm/test_<name>.py`; whole suite `./test/run.sh`.
- **Fail-safe:** every gate errs toward NOT acting. A pause/shadow bug may withhold action; it must never cause an unintended action.
- **Pause keeps the eyes open:** a paused relay still polls, shows state, and fires danger/escalation notifications. Pause only freezes the inject (`_handle` approve path) and swarm delivery (`_deliver`).
- **Shadow is never `active`:** `SessionInfo.active` stays `mode in ("safe","wild","insane")`. Shadow classifies + records but never injects, never notifies.
- **Mascot frame geometry:** the CRT screen interior is exactly 6 chars (`eyes`/`mid`/`mouth`).

---

### Task 1: Global pause - watcher (flag, toggle, gates)

**Files:**
- Modify: `iterm/watcher.py` (`__init__` state block ~L188; `_handle` approve path ~L452; `_deliver` top ~L590; new `toggle_pause`)
- Test: `iterm/test_watcher.py`

**Interfaces:**
- Produces: `Watcher.paused: bool`; `Watcher.toggle_pause() -> bool` (flips, audits `paused`/`resumed`, returns new state).

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_watcher.py` (new function before the `if __name__` block), and wire it into the runner:

```python
async def pause_tests():
    """Paused relay freezes the hands (no inject, no delivery) but keeps the
    eyes (still classifies + notifies danger). Resume restores acting."""
    from watcher import Watcher, SessionInfo
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    notify = {"n": 0}
    rows = []
    real_notify = W.notify_mac
    real_record = W.audit.record
    W.notify_mac = lambda *a, **k: notify.__setitem__("n", notify["n"] + 1)
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    try:
        w = Watcher(connection=None, dry_run=False)

        # PAUSED: an armed-safe tab with a safe prompt is NOT injected.
        w.paused = True
        fs = FakeSession()
        s = SessionInfo("s", title="x", _iterm_session=fs, mode="safe")
        w.sessions["s"] = s
        sraw, shard = _safe()
        await w._handle(s, sraw, shard)
        chk("paused: safe prompt not injected", fs.sent == [])
        chk("paused: not counted approved", s.n_approved == 0)

        # PAUSED: a dangerous prompt STILL notifies (eyes stay open).
        notify["n"] = 0
        fd = FakeSession()
        d = SessionInfo("d", title="d", _iterm_session=fd, mode="safe")
        w.sessions["d"] = d
        draw, dhard = _danger()
        await w._handle(d, draw, dhard)
        chk("paused: danger still notifies", notify["n"] == 1)
        chk("paused: danger never injects", fd.sent == [])

        # RESUME: the same safe prompt now injects.
        w.paused = False
        fs2 = FakeSession()
        s2 = SessionInfo("s2", title="x2", _iterm_session=fs2, mode="safe")
        w.sessions["s2"] = s2
        await w._handle(s2, sraw, shard)
        chk("resumed: safe prompt injected", fs2.sent == ["\r"])

        # toggle_pause flips state and records the transition.
        rows.clear()
        was = w.toggle_pause()
        chk("toggle_pause returns new state (paused)", was is True and w.paused)
        chk("pause audited", rows and rows[-1][0] == "paused")
        w.toggle_pause()
        chk("resume audited", rows[-1][0] == "resumed" and not w.paused)
    finally:
        W.notify_mac = real_notify
        W.audit.record = real_record

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

Add to the runner block:
```python
    r10 = asyncio.run(pause_tests())
    sys.exit(0 if (r1 and r2 and r3 and r4 and r5 and r6 and r7 and r8
                   and r9 and r10) else 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL - `AttributeError: 'Watcher' object has no attribute 'toggle_pause'` (and `.paused`).

- [ ] **Step 3: Implement the flag, toggle, and gates**

(a) In `Watcher.__init__`, in the swarm-state block (near `self._last_event = None`), add:
```python
        self.paused = False                # frozen hands (approvals+deliveries)
```

(b) Add the toggle method (near `toggle`/`set_mode`):
```python
    def toggle_pause(self) -> bool:
        """Freeze/unfreeze relay's HANDS (auto-approvals + swarm deliveries)
        while its eyes stay open (still watches + warns). Holds until toggled
        again - never auto-resumes. Records the transition. Returns new state."""
        self.paused = not self.paused
        audit.record("paused" if self.paused else "resumed", "relay", "", "")
        self._note("PAUSED - relay is NOT acting (approvals + deliveries frozen)"
                   if self.paused
                   else "resumed - relay is acting again")
        return self.paused
```

(c) In `_handle`, at the very start of the approve path (right after the `# --- approve path ---` comment block, before `verdict_reason = ...`), add:
```python
        # Paused: relay's hands are frozen - do not inject or audit an approval
        # (nothing happened). The NOTIFY branch above already ran, so danger is
        # never silenced by a pause; the PAUSED banner explains the stillness.
        if self.paused:
            info.state = "prompting"
            return
```

(d) In `_deliver`, right after the own-tab guard (`if info.session_id == self.own_sid: return`), add:
```python
        if self.paused:
            return  # hands frozen: the message stays queued, retries on resume
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_watcher.py`
Expected: PASS (all suites end `ALL PASS`).

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "feat(watcher): global pause - freeze approvals + deliveries, keep watching"
```

---

### Task 2: Global pause - TUI (key, banner, frozen mascot)

**Files:**
- Modify: `iterm/app.py` (`BINDINGS` ~L534; new `action_pause`; `effective_mascot_state` ~L200; `_MASCOT_COLOR` ~L328; `mascot_face_big` ~L231 + its state branches; `banner_with_face` ~L334; subtitle update ~L761; `_tick_reactor` banner call ~L848)
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `Watcher.paused` (Task 1).
- Produces: `effective_mascot_state(..., paused=False)` returns `"paused"` above ALL other states; `mascot_face_big(..., paused=False)`; `banner_with_face(..., paused=False)`; `action_pause` bound to `p`.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_app.py` inside `go()` (uses `chk`):
```python
    from app import effective_mascot_state as ems
    chk("paused outranks alarmed",
        ems("ok", awaiting=3, working=False, armed=2, paused=True) == "paused")
    chk("paused outranks a danger reaction",
        ems("ok", awaiting=0, working=False, armed=1,
            reaction="danger", paused=True) == "paused")
    chk("not paused -> normal ladder",
        ems("ok", awaiting=0, working=False, armed=2, paused=False) == "guarding")
    from app import mascot_face_big as mfb
    chk("paused frame shows a paused cue",
        any("paused" in line for line in mfb(0, "ok", armed=2, paused=True)))
    chk("paused frame is 6 lines and aligned",
        len(mfb(0, "ok", armed=2, paused=True)) == 6
        and all(mfb(0, "ok", armed=2, paused=True)[i][11] == "│"
                for i in (2, 3, 4)))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL - `effective_mascot_state() got an unexpected keyword argument 'paused'`.

- [ ] **Step 3: Add the paused state, color, and frame**

(a) `effective_mascot_state` - add `paused` param, top-ranked:
```python
def effective_mascot_state(band, *, awaiting, working, armed, reaction=None,
                           paused=False):
    """The state that drives the frame + color, folding in pause and a
    momentary reaction. Pause outranks EVERYTHING - a frozen relay is the one
    fact you must not miss. Then danger flinch, then the base ladder / done."""
    if paused:
        return "paused"
    if reaction == "danger":
        return "flinch"
    base = mascot_state(band, alarmed=awaiting > 0, working=working, armed=armed)
    if reaction == "done" and base not in ("alarmed", "critical"):
        return "celebrate"
    return base
```

(b) `_MASCOT_COLOR` - add `paused`:
```python
_MASCOT_COLOR = {"alarmed": WARN, "critical": DANGER,
                 "working": BRIGHT, "guarding": ACCENT, "idle": DIM,
                 "celebrate": BRIGHT, "flinch": WARN, "paused": CYAN}
```

(c) `mascot_face_big` - add `paused` param and thread it into the state call, then add a `paused` branch at the TOP of the state `if/elif` chain:
```python
def mascot_face_big(tick: int, band: str, *, awaiting: int = 0,
                    working: bool = False, armed: int = 0,
                    approvals: int = 0, reaction=None, paused: bool = False) -> list:
```
Change the `state = effective_mascot_state(...)` line inside it to pass `paused=paused`:
```python
    state = effective_mascot_state(band, awaiting=awaiting, working=working,
                                   armed=armed, reaction=reaction, paused=paused)
```
Add as the FIRST branch (before `if state == "celebrate":`):
```python
    if state == "paused":
        eyes, mid, mouth, beacon = " ▪  ▪ ", "      ", "  ══  ", "⏸"
        say = "paused"
    elif state == "celebrate":
```

(d) `banner_with_face` - add `paused` param and forward it to both calls:
```python
def banner_with_face(tick: int, band: str, *, awaiting: int = 0,
                     working: bool = False, armed: int = 0,
                     approvals: int = 0, reaction=None, paused: bool = False) -> str:
```
Update its `state = effective_mascot_state(...)` and `face = mascot_face_big(...)`:
```python
    state = effective_mascot_state(band, awaiting=awaiting, working=working,
                                   armed=armed, reaction=reaction, paused=paused)
    color = _MASCOT_COLOR[state]
    logo = BANNER.split("\n")
    face = mascot_face_big(tick, band, awaiting=awaiting, working=working,
                           armed=armed, approvals=approvals, reaction=reaction,
                           paused=paused)
```

- [ ] **Step 4: Wire the key, banner, and tick**

(a) Add to `BINDINGS` (after the `space` binding):
```python
        Binding("p", "pause", "Pause/resume acting"),
```

(b) Add the action (near `action_toggle`):
```python
    def action_pause(self) -> None:
        if self.watcher:
            self.watcher.toggle_pause()
            self._refresh()
```

(c) Subtitle banner - change the `#subtitle` update to prepend a loud PAUSED tag. Replace:
```python
        self.query_one("#subtitle", Static).update(
            f"[{DIM}]RELAY · SESSION CONTROL ·[/] "
```
with:
```python
        pause_tag = (f"[bold {WARN}]⏸ PAUSED - NOT acting[/] [{DIM}]·[/] "
                     if getattr(self.watcher, "paused", False) else "")
        self.query_one("#subtitle", Static).update(
            pause_tag +
            f"[{DIM}]RELAY · SESSION CONTROL ·[/] "
```
(leave the rest of the f-string args unchanged).

(d) `_tick_reactor` banner call - add `paused=`:
```python
            self.query_one("#banner", Static).update(banner_with_face(
                self._tick, label, awaiting=awaiting,
                working=self._tick < getattr(self, "_mascot_active_until", 0),
                armed=sum(1 for i in self.watcher.sessions.values()
                          if i.active and i.session_id != self._own_sid),
                approvals=approvals, reaction=reaction,
                paused=getattr(self.watcher, "paused", False)))
```

- [ ] **Step 5: Run to verify it passes**

Run: `python3 iterm/test_app.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): pause key + loud PAUSED banner + frozen mascot state"
```

---

### Task 3: Shadow-arm - watcher (mode, cycle, restore, dry-run branch)

**Files:**
- Modify: `iterm/watcher.py` (`MODES` ~L877; `_MODE_CYCLE` ~L876; restore tuple ~L554; `_handle` ~L400; new `toggle_shadow`)
- Test: `iterm/test_watcher.py`

**Interfaces:**
- Produces: `"shadow"` accepted as a mode (persisted/restored); `Watcher.toggle_shadow(sid)` (shadow <-> off); `_handle` records `would-approve`/`would-escalate` for a shadow tab and never injects/notifies. Space from `shadow` promotes to `safe` (`_MODE_CYCLE["shadow"] = "safe"`).

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_watcher.py` (new function; wire into runner as `r11`):
```python
async def shadow_tests():
    """Shadow tab records what safe WOULD do, never injects, never notifies."""
    from watcher import Watcher, SessionInfo
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    notify = {"n": 0}
    rows = []
    real_notify = W.notify_mac
    real_record = W.audit.record
    W.notify_mac = lambda *a, **k: notify.__setitem__("n", notify["n"] + 1)
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    try:
        w = Watcher(connection=None, dry_run=False)

        # Shadow + safe prompt -> would-approve, no inject, no notify.
        fs = FakeSession()
        s = SessionInfo("s", title="x", _iterm_session=fs, mode="shadow")
        w.sessions["s"] = s
        chk("shadow is not active", s.active is False)
        sraw, shard = _safe()
        await w._handle(s, sraw, shard)
        chk("shadow safe: would-approve recorded",
            rows and rows[-1][0] == "would-approve")
        chk("shadow safe: never injected", fs.sent == [])
        chk("shadow safe: never notified", notify["n"] == 0)
        chk("shadow safe: shows cleared", s.state == "cleared")
        # Debounce: same prompt does not re-record.
        n_before = len(rows)
        await w._handle(s, sraw, shard)
        chk("shadow: debounced (no re-record)", len(rows) == n_before)

        # Shadow + dangerous prompt -> would-escalate, still silent.
        rows.clear()
        notify["n"] = 0
        fd = FakeSession()
        d = SessionInfo("d", title="d", _iterm_session=fd, mode="shadow")
        w.sessions["d"] = d
        draw, dhard = _danger()
        await w._handle(d, draw, dhard)
        chk("shadow danger: would-escalate recorded",
            rows and rows[-1][0] == "would-escalate")
        chk("shadow danger: never notified", notify["n"] == 0)
        chk("shadow danger: never injected", fd.sent == [])

        # toggle_shadow flips shadow <-> off; Space from shadow -> safe.
        fz = FakeSession()
        z = SessionInfo("z", title="z", _iterm_session=fz, mode="off")
        w.sessions["z"] = z
        w.toggle_shadow("z")
        chk("toggle_shadow: off -> shadow", z.mode == "shadow")
        w.toggle("z")
        chk("Space from shadow -> safe", z.mode == "safe")
        w.toggle_shadow("z")
        chk("toggle_shadow: from any -> shadow", z.mode == "shadow")
        w.toggle_shadow("z")
        chk("toggle_shadow: shadow -> off", z.mode == "off")
    finally:
        W.notify_mac = real_notify
        W.audit.record = real_record

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

Wire into the runner (with the Task 1 `r10`):
```python
    r10 = asyncio.run(pause_tests())
    r11 = asyncio.run(shadow_tests())
    sys.exit(0 if (r1 and r2 and r3 and r4 and r5 and r6 and r7 and r8
                   and r9 and r10 and r11) else 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL - `toggle_shadow` missing / no `would-escalate` recorded.

- [ ] **Step 3: Implement mode plumbing + the shadow branch**

(a) `MODES` - add `shadow`:
```python
    MODES = ("off", "safe", "wild", "insane", "shadow")
```

(b) `_MODE_CYCLE` - add the promotion `shadow -> safe`:
```python
    _MODE_CYCLE = {"off": "safe", "safe": "wild", "wild": "insane",
                   "insane": "off", "shadow": "safe"}
```

(c) Restore tuple (the persisted-mode restore, ~L554) - accept `shadow`:
```python
                    if stored in ("safe", "wild", "insane", "shadow") and \
                            self.sessions[sid].mode == "off":
```

(d) `toggle_shadow` (near `toggle`/`set_mode`):
```python
    def toggle_shadow(self, sid: str) -> None:
        """Toggle a tab between shadow (per-tab dry-run) and off. Shadow is a
        deliberate calibration mode, so it is its own key, not in the Space
        cycle (Space promotes shadow -> safe)."""
        if self._armable(sid):
            info = self.sessions[sid]
            info.mode = "off" if info.mode == "shadow" else "shadow"
            info._last_prompt_id = None
            self._persist_mode(sid, info.mode)
```

(e) The shadow branch in `_handle` - insert AFTER the display-state block
(`if decision.action == Action.NOTIFY: ... elif ... Action.INJECT: ...`) and
BEFORE `if not info.active:`:
```python
        # Shadow: a per-tab dry-run of the SAFE pipeline. Record what relay
        # WOULD do (would-approve on a safe prompt, would-escalate otherwise)
        # but never inject and never alarm - nothing real happened, you are
        # watching. Reuses safe's predicate (INJECT == would-approve).
        if info.mode == "shadow" and info.session_id != self.own_sid:
            if decision.prompt_id is not None and \
                    decision.prompt_id == info._last_prompt_id:
                return
            info._last_prompt_id = decision.prompt_id
            if decision.action == Action.INJECT:
                info.state = "cleared"
                audit.record("would-approve", info.title,
                             decision.command or "",
                             f"shadow ({decision.reason})")
            else:
                info.state = "blocked"
                audit.record("would-escalate", info.title,
                             decision.command or "", decision.reason)
            return
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_watcher.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "feat(watcher): shadow-arm - per-tab dry-run of the safe pipeline"
```

---

### Task 4: Shadow-arm - surfacing (key, glyphs, pane)

**Files:**
- Modify: `iterm/app.py` (`BINDINGS`; new `action_shadow`; `_update_preview` shadow banner ~L858)
- Modify: `iterm/titles.py` (`MODE_GLYPH`/`MODE_WORD` ~L16; `_PREFIX_RE` ~L28)
- Modify: `iterm/statusbar.py` (`MODE_CIRCLE` ~L24)
- Test: `iterm/test_titles.py`, `iterm/test_statusbar.py`

**Interfaces:**
- Consumes: `Watcher.toggle_shadow` (Task 3).
- Produces: `s` bound to `action_shadow`; `shadow` glyph in titles + status-bar badge; a "SHADOW" marker in the live-feed pane.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_titles.py` (follow its `check`/`chk` idiom):
```python
    ok &= check("shadow renders its glyph prefix",
                titles.MODE_GLYPH.get("shadow") == "◌"
                and "◌" in titles.render("glyphs", "shadow", "idle", False, "api"))
    ok &= check("shadow prefix is strippable (crash-safety)",
                titles.strip_prefix("◌ api") == "api")
```

Add to `iterm/test_statusbar.py`:
```python
    ok &= check("shadow badge uses its own circle",
                statusbar.MODE_CIRCLE.get("shadow") == "\U0001f535"
                and statusbar.label("shadow")
                == f"{statusbar.MODE_CIRCLE['shadow']} RELAY:shadow")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 iterm/test_titles.py` and `python3 iterm/test_statusbar.py`
Expected: FAIL - shadow not in the glyph/circle maps.

- [ ] **Step 3: Add the glyphs**

(a) `iterm/titles.py` - add shadow to both maps:
```python
MODE_GLYPH = {"safe": "◉", "wild": "▲", "insane": "✦", "shadow": "◌"}
MODE_WORD = {"safe": "SAFE", "wild": "WILD", "insane": "INSANE",
             "shadow": "SHADOW"}
```
And extend `_PREFIX_RE` so a shadow prefix is stripped (crash-safety):
```python
_PREFIX_RE = re.compile(
    r"^[◉▲✦◌]?[‼⊘⧗]?"
    r"(?:\[(?:SAFE|WILD|INSANE|SHADOW|AWAITING|BLOCKED|STALE)\]){0,2}"
    r" ")
```

(b) `iterm/statusbar.py` - add shadow to `MODE_CIRCLE`:
```python
MODE_CIRCLE = {
    "off":    "⚪",       # white circle
    "safe":   "\U0001f7e2",   # green circle
    "wild":   "\U0001f7e1",   # yellow circle
    "insane": "\U0001f534",   # red circle
    "shadow": "\U0001f535",   # blue circle - observing, not acting
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 iterm/test_titles.py` and `python3 iterm/test_statusbar.py`
Expected: PASS.

- [ ] **Step 5: Add the key and the pane marker**

(a) `iterm/app.py` `BINDINGS` (after the `p` binding from Task 2):
```python
        Binding("s", "shadow", "Shadow-arm (dry-run this tab)"),
```

(b) `action_shadow` (near `action_toggle`/`action_pause`):
```python
    def action_shadow(self) -> None:
        sid = self._selected_sid()
        if not (sid and self.watcher):
            return
        if sid == self._own_sid:
            self.query_one(Log).write_line(
                "shadow: relay never acts on its own panel tab")
            return
        self.watcher.toggle_shadow(sid)
        self._refresh()
```

(c) `_update_preview` shadow marker - in the header build (where the `why`
line is added, ~L910), make the WHY line and a banner reflect shadow. Just
BEFORE the `header = (...)` assignment, add:
```python
        shadow = " ◌ SHADOW - previewing, relay is NOT acting on this tab\n" \
            if info.mode == "shadow" else ""
```
and insert `f"{shadow}"` into the header f-string right after the `MODE:` line
(before `{why}`):
```python
                  f" MODE:{mode}  LINK:{loc}  "
                  f"CLEARED:{info.n_approved}  HELD:{info.n_escalated}\n"
                  f"{shadow}"
                  f"{why}"
```

- [ ] **Step 6: Run the full suite**

Run: `./test/run.sh`
Expected: ends `ALL SUITES PASSED`.

- [ ] **Step 7: Commit**

```bash
git add iterm/app.py iterm/titles.py iterm/statusbar.py iterm/test_titles.py iterm/test_statusbar.py
git commit -m "feat(tui): shadow key + shadow glyph in titles/badge + pane marker"
```

---

## Self-Review

**1. Spec coverage:**
- Global pause: flag + toggle + gates (Task 1); key + loud banner + frozen mascot (Task 2). ✓
- Shadow-arm: mode + cycle + restore + `_handle` branch (Task 3); key + glyphs + pane (Task 4). ✓
- Pause keeps eyes open (still notifies danger) - Task 1 test asserts it. ✓
- Shadow never acts/notifies, records `would-approve`/`would-escalate` - Task 3 test asserts it. ✓
- Fail-safe (both err toward not acting) - both gates `return` without injecting. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code; tests show assertions. ✓

**3. Type consistency:** `paused` param threads identically through `effective_mascot_state`/`mascot_face_big`/`banner_with_face`; `toggle_pause`/`toggle_shadow`/`Watcher.paused` names match across defining and consuming tasks; `"shadow"` is added to `MODES`, `_MODE_CYCLE`, the restore tuple, `MODE_GLYPH`, `MODE_WORD`, `_PREFIX_RE`, and `MODE_CIRCLE` consistently. ✓

**Note:** the frozen-mascot frame width (Task 2) is regression-tested with the same `[11] == "│"` invariant added for Legible Relay, so a wrong-width paused frame is caught.
