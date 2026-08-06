# Extreme Insane Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/specs/2026-08-06-extreme-insane-mode-design.md` - read it first.

**Goal:** A fifth per-session mode, `extreme`, that auto-pushes an idle session
with a configured continuation prompt, budget-capped, armed only via `E E` in
the relay TUI, and gone on relay restart.

**Architecture:** `extreme` joins the mode vocabulary as a strict superset of
`insane` (same permission auto-approval, plus idle pushes). The prompt and the
remaining fire budget are in-memory fields on `SessionInfo` - never in the DB;
`_persist_mode` writes `"insane"` for an extreme session so a restart restores
the insane floor. A new watcher method `_fire_extreme` runs per tick with the
same gates as inbox delivery plus a dwell and an empty-input-line check, and
injects via the timer-style `payload + sleep(0.3) + "\r"` path.

**Tech Stack:** Python 3, Textual TUI, iTerm2 python API, sqlite. No pytest -
each suite is a plain `__main__` runner executed by `./test/run.sh` (globs
`iterm/test_*.py`).

## Global Constraints

- NEVER use the em-dash character (U+2014) anywhere - plain ASCII `-` only.
- Commit messages: repo style (`feat(watcher): ...`), NO `Co-Authored-By` line.
- No DB schema changes. `sessions.mode` must never store `"extreme"`.
- `ARM_REQUEST_MODES` in `iterm/db.py` stays `("safe", "wild", "insane")`.
- `Watcher.MODES` and `_MODE_CYCLE` stay unchanged (extreme is not in the
  Space cycle and `set_mode` keeps refusing it).
- Mode glyphs must be SINGLE-WIDTH terminal cells. Extreme's glyph is `✷`
  everywhere (TUI, tab titles, swarm map). Never an emoji in those maps.
- Config defaults: `extreme_fires = 5` (int, min 1), `extreme_dwell = 45.0`
  seconds (float, min 0).
- All new tests go in a NEW suite `iterm/test_extreme.py` (auto-discovered by
  `./test/run.sh`); existing test files are not modified.
- Tests must be hermetic: `os.environ["RELAY_CONFIG"]` points at a nonexistent
  or temp path before importing relay modules (see `iterm/test_watcher.py:17`).
- Run a task's suite with `python3 iterm/test_extreme.py`; before the final
  commit of the last task run the whole suite: `./test/run.sh`.

---

### Task 1: `swarm.prompt_line_empty()` - the draft-guard helper

**Files:**
- Modify: `iterm/swarm.py` (below `claude_prompt_ready`, ~line 396)
- Create: `iterm/test_extreme.py`

**Interfaces:**
- Produces: `swarm.prompt_line_empty(lines: List[str]) -> bool` - True when
  the Claude input box row is present in the screen tail and contains no
  typed text. Task 4's `_fire_extreme` calls it as a fire gate.
- Consumes: existing module-private `_INPUT_BOX_RE` and `_BOX_GLYPHS` in
  `iterm/swarm.py` (~lines 361-363).

- [ ] **Step 1: Create the suite skeleton with failing tests**

Create `iterm/test_extreme.py`:

```python
"""Extreme insane mode suite: the draft-guard helper, config knobs, watcher
arming/firing/exhaustion, TUI + statusbar chrome.

Run: python3 iterm/test_extreme.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"

ok = True


def chk(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond


# Screen tails. READY is a Claude idle screen with an EMPTY input box;
# DRAFT has operator text typed after the '>' but not submitted.
READY = ["╭──────────────╮", "│ >            │", "╰──────────────╯",
         "  ? for shortcuts"]
DRAFT = ["╭──────────────╮", "│ > fix the login bug   │",
         "╰──────────────╯", "  ? for shortcuts"]
SHELL = ["some output", "~/Work $"]


def test_prompt_line_empty():
    import swarm
    chk("empty input box -> True", swarm.prompt_line_empty(READY))
    chk("typed draft -> False", not swarm.prompt_line_empty(DRAFT))
    chk("shell prompt (no box) -> False", not swarm.prompt_line_empty(SHELL))
    chk("empty screen -> False", not swarm.prompt_line_empty([]))
    chk("READY still passes claude_prompt_ready",
        swarm.claude_prompt_ready(READY))


if __name__ == "__main__":
    test_prompt_line_empty()
    print("ALL PASSED" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: `AttributeError: module 'swarm' has no attribute 'prompt_line_empty'`

- [ ] **Step 3: Implement the helper**

In `iterm/swarm.py`, directly below `claude_prompt_ready` (~line 396):

```python
def prompt_line_empty(lines: List[str]) -> bool:
    """True when Claude's input box row is visibly EMPTY - no operator draft.

    An extreme push types text and presses Enter; landing on a half-typed
    message would append to it and SUBMIT it. So the input row ("│ > ...")
    must exist in the ready tail and carry nothing after the '>'. No input
    row found => not a known-empty box => False (fail safe: no push)."""
    tail = [l for l in lines if l.strip()][-3:]
    for l in reversed(tail):
        if _INPUT_BOX_RE.match(l):
            rest = _INPUT_BOX_RE.sub("", l, count=1)
            return rest.strip("".join(_BOX_GLYPHS) + " \t") == ""
    return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_extreme.py`
Expected: 5x PASS, `ALL PASSED`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_extreme.py
git commit -m "feat(swarm): prompt_line_empty - is the input box free of a draft"
```

---

### Task 2: Config knobs `extreme_fires` + `extreme_dwell`

**Files:**
- Modify: `iterm/config.py` (dataclass ~line 61-70, `load()` swarm block
  ~line 127-129 and result construction ~line 223-232, `dump()` swarm text
  ~line 252-254)
- Modify: `iterm/test_extreme.py`

**Interfaces:**
- Produces: `Config.extreme_fires: int = 5`, `Config.extreme_dwell: float
  = 45.0`, parsed from `[swarm]`, rendered by `dump()`. Task 4's watcher
  reads them via `getattr(cfg, ...)`.
- Consumes: existing `_get_float(cp, section, key, fallback, warns)`.

- [ ] **Step 1: Add failing tests**

In `iterm/test_extreme.py`, add below `test_prompt_line_empty` and call it
from `__main__` (each later task extends this runner the same way):

```python
def test_config_knobs():
    import tempfile
    import config as C
    d = C.Config()
    chk("default extreme_fires = 5", d.extreme_fires == 5)
    chk("default extreme_dwell = 45.0", d.extreme_dwell == 45.0)
    path = os.path.join(tempfile.mkdtemp(), "config")
    with open(path, "w") as f:
        f.write("[swarm]\nextreme_fires = 3\nextreme_dwell = 10\n")
    cfg, warns = C.load(path)
    chk("parses extreme_fires = 3", cfg.extreme_fires == 3)
    chk("parses extreme_dwell = 10.0", cfg.extreme_dwell == 10.0)
    with open(path, "w") as f:
        f.write("[swarm]\nextreme_fires = 0\nextreme_dwell = -5\n")
    cfg2, _ = C.load(path)
    chk("extreme_fires clamps to >= 1", cfg2.extreme_fires == 1)
    chk("extreme_dwell clamps to >= 0", cfg2.extreme_dwell == 0.0)
    chk("dump() round-trips the knobs",
        "extreme_fires" in C.dump(cfg) and "extreme_dwell" in C.dump(cfg))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: FAIL on `default extreme_fires` (AttributeError on the dataclass).

- [ ] **Step 3: Implement**

In the `Config` dataclass, after `spawn_arm: str = "off"` (~line 63):

```python
    extreme_fires: int = 5       # pushes per E E arming (TUI extreme mode)
    extreme_dwell: float = 45.0  # seconds idle before an extreme push
```

In `load()`, after the `cooldown = _get_float(...)` lines (~line 129):

```python
    e_fires = max(1, int(_get_float(cp, "swarm", "extreme_fires",
                                    float(d.extreme_fires), warns)))
    e_dwell = max(0.0, _get_float(cp, "swarm", "extreme_dwell",
                                  d.extreme_dwell, warns))
```

In the `Config(...)` construction (~line 223), after `spawn_arm=arm,`:

```python
        extreme_fires=e_fires,
        extreme_dwell=e_dwell,
```

In `dump()`, after the `spawn_arm` line (~line 254):

```python
        f"extreme_fires   = {cfg.extreme_fires}\n"
        f"extreme_dwell   = {cfg.extreme_dwell:g}\n"
```

(Match the exact f-string/concatenation style of the surrounding lines.)

- [ ] **Step 4: Run both suites**

Run: `python3 iterm/test_extreme.py` then `python3 iterm/test_config.py`
Expected: both `ALL PASSED` / exit 0 (config suite must not regress).

- [ ] **Step 5: Commit**

```bash
git add iterm/config.py iterm/test_extreme.py
git commit -m "feat(config): extreme_fires and extreme_dwell knobs"
```

---

### Task 3: Watcher arming plumbing - extreme as a mode value

**Files:**
- Modify: `iterm/watcher.py` (SessionInfo ~line 74-110, `__init__`
  ~line 250, `_handle` ~line 555, `_fire_timers` ~line 859, arm_request
  honor site ~line 663-691, `_persist_mode` ~line 1364, new methods after
  `set_all` ~line 1362)
- Modify: `iterm/titles.py` (~line 16-17), `iterm/swarm.py` (~line 639 and
  ~line 694)
- Modify: `iterm/test_extreme.py`

**Interfaces:**
- Produces:
  - `SessionInfo.extreme_prompt: str = ""`, `SessionInfo.extreme_fires_left:
    int = 0`, `SessionInfo._idle_since: float` (repr=False field).
  - `Watcher.extreme_fires: int`, `Watcher.extreme_dwell: float` (from cfg).
  - `Watcher.set_extreme(sid: str, prompt: str) -> bool` - arms extreme on
    an insane (or re-arms an extreme) session; refills the budget from
    `self.extreme_fires`; returns success. TUI Task 5 calls this.
  - `Watcher.clear_extreme(sid: str) -> bool` - extreme -> insane. TUI
    Task 5 calls this.
  - `SessionInfo(mode="extreme").active == True`.
- Consumes: `swarmdb.ARM_REQUEST_MODES`, `_persist_mode`, `_armable`.

- [ ] **Step 1: Add failing tests**

Append to `iterm/test_extreme.py` (and call from `__main__`):

```python
class FakeSession:
    def __init__(self):
        self.sent = []

    async def async_send_text(self, t):
        self.sent.append(t)


def _mk_watcher():
    import watcher as W
    W.notify_mac = lambda *a, **k: None
    W.audit.record = lambda *a, **k: True
    return W, W.Watcher(connection=None, dry_run=False)


def test_arming_plumbing():
    W, w = _mk_watcher()
    chk("watcher reads extreme_fires from cfg default",
        w.extreme_fires == 5)
    chk("watcher reads extreme_dwell from cfg default",
        w.extreme_dwell == 45.0)

    info = W.SessionInfo("s1", title="t1", _iterm_session=FakeSession(),
                         mode="insane")
    w.sessions["s1"] = info
    chk("set_extreme refuses a safe session",
        not (setattr(info, "mode", "safe") or w.set_extreme("s1", "push")))
    info.mode = "insane"
    chk("set_extreme refuses an empty prompt",
        not w.set_extreme("s1", "   "))
    chk("set_extreme arms an insane session", w.set_extreme("s1", "push on"))
    chk("mode is extreme", info.mode == "extreme")
    chk("budget filled from config", info.extreme_fires_left == 5)
    chk("prompt stored stripped", info.extreme_prompt == "push on")
    chk("extreme session is active (armed)", info.active)
    chk("re-arm while extreme is allowed", w.set_extreme("s1", "push more"))
    chk("clear_extreme drops to insane",
        w.clear_extreme("s1") and info.mode == "insane")
    chk("prompt survives disarm (re-arm convenience)",
        info.extreme_prompt == "push more")
    chk("clear_extreme on a non-extreme session is False",
        not w.clear_extreme("s1"))
    w.own_sid = "s1"
    info.mode = "insane"
    chk("set_extreme never arms relay's own tab",
        not w.set_extreme("s1", "push"))
    w.own_sid = None
    chk("extreme is not spawn-requestable",
        "extreme" not in W.swarmdb.ARM_REQUEST_MODES)
    chk("extreme is not set_mode-able (MODES unchanged)",
        "extreme" not in w.MODES)
    chk("extreme is not in the SPACE cycle",
        "extreme" not in w._MODE_CYCLE)


def test_extreme_approves_like_insane():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = W.SessionInfo("s2", title="t2", _iterm_session=fs, mode="extreme")
    w.sessions["s2"] = info
    raw = [" Bash command", "", "   grep foo src/", "   search", "",
           "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    asyncio.run(w._handle(info, raw, [True] * len(raw)))
    chk("extreme approves a permission prompt (insane superset)",
        fs.sent == ["\r"])


def test_persist_maps_extreme_to_insane():
    W, w = _mk_watcher()
    stored = {}
    W.swarmdb.set_session_mode = \
        lambda conn, name, mode: stored.__setitem__(name, mode)
    w._swarm_conn = lambda: None
    w.registry["s3"] = {"name": "worker3"}
    w.sessions["s3"] = W.SessionInfo("s3", title="t3", mode="insane")
    w._persist_mode("s3", "extreme")
    chk("persisting extreme writes 'insane'", stored.get("worker3") == "insane")
    w._persist_mode("s3", "wild")
    chk("other modes persist verbatim", stored.get("worker3") == "wild")
```

Note: `Watcher.__init__` may name the own-tab attribute `own_sid`; check the
constructor and use the real attribute name in the own-tab test.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: FAIL at `watcher reads extreme_fires` (no such attribute).

- [ ] **Step 3: Implement**

`iterm/watcher.py`:

1. `SessionInfo` - extend the mode docstring (~line 87) and add fields after
   `stale` (~line 99):

```python
    #   "extreme" - insane PLUS: when idle at an empty ready prompt past a
    #              dwell, relay pushes `extreme_prompt` (budget-capped,
    #              in-memory only - a relay restart disarms back to insane).
```

```python
    extreme_prompt: str = ""         # extreme: text pushed into an idle tab
    extreme_fires_left: int = 0      # extreme: remaining push budget
    _idle_since: float = field(default=0.0, repr=False)  # idle dwell anchor
```

2. `active` property (~line 110): `("safe", "wild", "insane", "extreme")`.

3. `__init__`, near `self.notify_cooldown = cfg.notify_cooldown` (~line 250):

```python
        self.extreme_fires = max(1, int(getattr(cfg, "extreme_fires", 5)))
        self.extreme_dwell = float(getattr(cfg, "extreme_dwell", 45.0))
```

4. `_handle` mode branch (~line 555): `if info.mode in ("insane", "extreme"):`
   and update the comment block above it (extreme = insane superset).

5. `_fire_timers` armed check (~line 859):
   `armed = info.mode in ("safe", "wild", "insane", "extreme")`.

6. arm_request honor site (~line 667-685): honor only whitelisted requests;
   anything else (including a DB-injected `"extreme"`) takes the refusal
   branch. Change `if within:` to
   `if within and req in swarmdb.ARM_REQUEST_MODES:`.

7. `_persist_mode` (~line 1373): extreme is in-memory only - store its floor:

```python
            swarmdb.set_session_mode(self._swarm_conn(), reg["name"],
                                     "insane" if mode == "extreme" else mode)
```

   Extend the docstring: extreme never persists; a restart restores insane.
   (The first-sight restore whitelist at ~line 701 already lacks "extreme" -
   leave it untouched.)

8. New methods after `set_all` (~line 1362):

```python
    def set_extreme(self, sid: str, prompt: str) -> bool:
        """Arm EXTREME (TUI-only surface - no CLI/status-bar/spawn path).
        Requires an already-insane session: extreme is an escalation the
        operator performs by hand, never a jump from cold. Refills the push
        budget from config. In-memory only - see _persist_mode."""
        if not self._armable(sid):
            return False
        info = self.sessions[sid]
        if info.mode not in ("insane", "extreme") or not prompt.strip():
            return False
        info.extreme_prompt = prompt.strip()
        info.extreme_fires_left = self.extreme_fires
        info.mode = "extreme"
        info._idle_since = 0.0
        info._last_prompt_id = None
        self._persist_mode(sid, "extreme")
        self._note(f"EXTREME armed on {info.title}: "
                   f"{self.extreme_fires} push(es)")
        return True

    def clear_extreme(self, sid: str) -> bool:
        """Extreme -> insane. Keeps the stored prompt so a re-arm can
        prefill it."""
        info = self.sessions.get(sid)
        if info is None or info.mode != "extreme":
            return False
        info.mode = "insane"
        info.extreme_fires_left = 0
        info._last_prompt_id = None
        self._persist_mode(sid, "insane")
        self._note(f"EXTREME disarmed on {info.title} - back to INSANE")
        return True
```

`iterm/titles.py` (~line 16-17): add `"extreme": "✷"` to `MODE_GLYPH` and
`"extreme": "EXTREME"` to `MODE_WORD`.

`iterm/swarm.py`: `_MODE_GLYPH` (~line 639) gains `"extreme": "✷"`; the armed
summary tuple (~line 694) becomes `("safe", "wild", "insane", "extreme")`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_extreme.py`, then `python3 iterm/test_watcher.py`
and `python3 iterm/test_titles.py` (no regressions).
Expected: all `ALL PASSED` / exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/titles.py iterm/swarm.py iterm/test_extreme.py
git commit -m "feat(watcher): extreme mode - insane superset, TUI-only, never persisted"
```

---

### Task 4: `_fire_extreme` - the idle push

**Files:**
- Modify: `iterm/watcher.py` (`_deliver` ~line 757-833, poll loop ~line
  344-356, new method after `_fire_timers` ~line 891)
- Modify: `iterm/test_extreme.py`

**Interfaces:**
- Consumes: `swarm.prompt_line_empty` (Task 1), `swarm.claude_prompt_ready`,
  `SessionInfo` extreme fields (Task 3), `audit.record`, `notify_mac`.
- Produces: `Watcher._fire_extreme(info) -> None`; `_deliver` now returns
  `True` when it injected a batch this tick (else `None`).

- [ ] **Step 1: Add failing tests**

Append to `iterm/test_extreme.py` (uses READY/DRAFT from Task 1; call all
from `__main__`):

```python
def _extreme_info(W, fs, *, dwell_ok=True):
    info = W.SessionInfo("x1", title="ex", _iterm_session=fs,
                         mode="extreme", state="idle")
    info.extreme_prompt = "keep going"
    info.extreme_fires_left = 2
    info.last_screen = list(READY)
    info._idle_since = time.time() - (999 if dwell_ok else 0)
    return info


def test_extreme_fires():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("push sends prompt then Enter", fs.sent == ["keep going", "\r"])
    chk("budget decremented", info.extreme_fires_left == 1)
    chk("idle anchor reset after fire", info._idle_since == 0.0)


def test_extreme_gates():
    W, w = _mk_watcher()

    fs = FakeSession()
    info = _extreme_info(W, fs)
    info.last_screen = list(DRAFT)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("draft on the input line blocks the push", fs.sent == [])

    fs2 = FakeSession()
    i2 = _extreme_info(W, fs2)
    i2._idle_since = time.time()  # just went idle: dwell not elapsed
    asyncio.run(w._fire_extreme(i2))
    chk("dwell not elapsed blocks the push", fs2.sent == [])

    fs3 = FakeSession()
    i3 = _extreme_info(W, fs3)
    i3.state = "working"
    asyncio.run(w._fire_extreme(i3))
    chk("non-idle state blocks and resets the anchor",
        fs3.sent == [] and i3._idle_since == 0.0)

    fs4 = FakeSession()
    i4 = _extreme_info(W, fs4)
    w.paused = True
    asyncio.run(w._fire_extreme(i4))
    chk("paused blocks the push", fs4.sent == [])
    w.paused = False

    fs5 = FakeSession()
    i5 = _extreme_info(W, fs5)
    i5.extreme_fires_left = 0
    asyncio.run(w._fire_extreme(i5))
    chk("zero budget never fires", fs5.sent == [])

    fs6 = FakeSession()
    i6 = _extreme_info(W, fs6)
    i6.mode = "insane"
    asyncio.run(w._fire_extreme(i6))
    chk("non-extreme mode never fires", fs6.sent == [])

    fs7 = FakeSession()
    i7 = _extreme_info(W, fs7)
    w.own_sid = "x1"
    asyncio.run(w._fire_extreme(i7))
    chk("own tab never fires", fs7.sent == [])
    w.own_sid = None

    fs8 = FakeSession()
    i8 = _extreme_info(W, fs8)
    w.registry["x1"] = {"name": "wx"}
    W.swarmdb.undelivered = lambda conn, name: [{"id": 1}]
    w._swarm_conn = lambda: None
    asyncio.run(w._fire_extreme(i8))
    chk("queued inbox mail blocks the push", fs8.sent == [])
    del w.registry["x1"]


def test_extreme_audit_before_act():
    W, w = _mk_watcher()
    W.audit.record = lambda *a, **k: False   # durable write fails
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("audit failure means NO push", fs.sent == [])
    chk("audit failure keeps the budget", info.extreme_fires_left == 2)
    W.audit.record = lambda *a, **k: True


def test_extreme_exhaustion():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = _extreme_info(W, fs)
    info.extreme_fires_left = 1
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("last push still sends", fs.sent == ["keep going", "\r"])
    chk("exhaustion reverts to insane", info.mode == "insane")
    chk("exhaustion noted in the log",
        any("exhausted" in l for l in w.log))


def test_extreme_dry_run():
    W, w = _mk_watcher()
    w.dry_run = True
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("dry-run never sends", fs.sent == [])
    chk("dry-run keeps the budget", info.extreme_fires_left == 2)
    chk("dry-run resets the anchor (no per-tick spam)",
        info._idle_since == 0.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: `AttributeError: 'Watcher' object has no attribute '_fire_extreme'`

- [ ] **Step 3: Implement**

`iterm/watcher.py`:

1. `_deliver`: after the successful batch send + mark_delivered block
   (~line 833, after the `DELIVER ->` note), add `return True`. Update its
   docstring's last line: returns True only when a batch was injected.

2. New method after `_fire_timers` (~line 891):

```python
    async def _fire_extreme(self, info: SessionInfo) -> None:
        """EXTREME push: keep an idle extreme-mode session moving by typing
        its configured prompt. Fires ONLY on `idle` - a blocked session (a
        genuine question) or a permission prompt is never touched here; the
        _handle gates own those. Additional gates: ready AND EMPTY input
        box (never append to an operator draft), empty inbox (queued mail
        wins the idle window), dwell elapsed, budget left, not paused, not
        relay's own tab. Audit BEFORE the send, like every injection."""
        if info.mode != "extreme" or info.session_id == self.own_sid:
            return
        if info.state != "idle" or \
                not swarm.claude_prompt_ready(info.last_screen):
            info._idle_since = 0.0
            return
        now = time.time()
        if info._idle_since == 0.0:
            info._idle_since = now
        if self.paused or info._iterm_session is None:
            return
        if info.extreme_fires_left <= 0:
            return
        if not swarm.prompt_line_empty(info.last_screen):
            return   # operator draft on the input line - hands off
        if now - info._idle_since < self.extreme_dwell:
            return
        reg = self.registry.get(info.session_id)
        if reg:
            try:
                if swarmdb.undelivered(self._swarm_conn(), reg["name"]):
                    return   # _deliver will spend this idle window instead
            except Exception as e:
                self._note(f"swarm db error: {e}")
                return
        if self.dry_run:
            audit.record("would-push", info.title,
                         info.extreme_prompt[:500], "extreme (dry-run)")
            self._note(f"DRY-RUN would push {info.title}")
            info._idle_since = 0.0
            return
        # LOG BEFORE ACT (same contract as approvals and timers).
        if not audit.record("extreme-pushed", info.title,
                            info.extreme_prompt[:500],
                            f"extreme ({info.extreme_fires_left - 1} left)"):
            if now - info._last_notify_ts >= self.notify_cooldown:
                info._last_notify_ts = now
                self._note(f"AUDIT-FAIL: not pushing {info.title}")
            return
        await info._iterm_session.async_send_text(info.extreme_prompt)
        await asyncio.sleep(0.3)
        await info._iterm_session.async_send_text("\r")
        info.extreme_fires_left -= 1
        info._idle_since = 0.0
        self._note(f"EXTREME push -> {info.title} "
                   f"({info.extreme_fires_left} left)")
        if info.extreme_fires_left == 0:
            info.mode = "insane"
            info._last_prompt_id = None
            self._persist_mode(info.session_id, "insane")
            self._last_event = ("done", time.time())
            self._note(f"EXTREME exhausted on {info.title} - back to INSANE")
            notify_mac(f"Relay - {info.title}",
                       "extreme budget exhausted - back to insane",
                       self.done_sound, session_id=info.session_id)
```

   (Check the constructor for the exact own-tab attribute name - `own_sid` -
   and mirror `_deliver`'s usage.)

3. Poll loop (~line 344-356): capture delivery and chain the push after the
   timers, still inside the fresh-snapshot branch:

```python
                    try:
                        res = await self._snapshot(info)
                        delivered = False
                        if res:
                            await self._handle(info, *res)
                            # Only deliver on fresh screen evidence this tick -
                            # a failed snapshot leaves state/last_screen stale,
                            # which must not be used to decide a delivery.
                            delivered = bool(await self._deliver(info))
                        # Staleness must be evaluated even on a failed screen
                        # read - a hung session is exactly the stale case.
                        self._check_stale(info)
                        await self._apply_title(info)
                        await self._fire_timers(info)
                        # Extreme push last, and never on a tick that already
                        # injected a delivery - one injection per tick.
                        if res and not delivered:
                            await self._fire_extreme(info)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_extreme.py`, then `python3 iterm/test_watcher.py`.
Expected: all `ALL PASSED` / exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/test_extreme.py
git commit -m "feat(watcher): extreme pushes an idle session - dwell, draft guard, budget"
```

---

### Task 5: TUI - `E E` arming form, mode cell, key bar, help

**Files:**
- Modify: `iterm/app.py` (MODE_STYLE ~line 108, KEYBAR ~line 128, help_text
  ~line 599, BINDINGS ~line 849, `__init__` flags ~line 913, mode cell
  ~line 1059, `on_input_submitted` ~line 1713, `action_dismiss_view`
  (search for it), new action + form methods next to the other
  double-press actions ~line 1963)
- Modify: `iterm/test_extreme.py`

**Interfaces:**
- Consumes: `Watcher.set_extreme` / `Watcher.clear_extreme` (Task 3),
  `Watcher.extreme_fires`, `SessionInfo.extreme_prompt` /
  `.extreme_fires_left`, the `_CONFIRM_WINDOW` arming pattern, the timer
  form's Input mount/close pattern (~line 1664-1682).
- Produces: binding `E` -> `action_extreme`; `self._extreme_armed: bool`,
  `self._extreme_form: None | {"sid": str}`; Input widget id
  `#extreme_prompt`.

- [ ] **Step 1: Add failing tests**

Append to `iterm/test_extreme.py` (call from `__main__`):

```python
def test_tui_chrome():
    import app as appmod
    chk("MODE_STYLE has an extreme entry",
        appmod.MODE_STYLE.get("extreme") == ("✷", "EXTREME", appmod.DANGER))
    chk("E is bound",
        any(getattr(b, "key", "") == "E" for b in appmod.RelayApp.BINDINGS))
    chk("keybar advertises E×2", "E×2" in appmod.KEYBAR)
    chk("help covers extreme", "EXTREME" in appmod.help_text())
    chk("action_extreme exists", hasattr(appmod.RelayApp, "action_extreme"))
    chk("form open/save/close helpers exist",
        hasattr(appmod.RelayApp, "_extreme_form_open")
        and hasattr(appmod.RelayApp, "_extreme_form_save")
        and hasattr(appmod.RelayApp, "_extreme_form_close"))
```

(Interactive double-press + form flow is verified by hand, same stance as the
zap spec: "Live two-press deferred to human". The logic that matters -
set/clear/fire - is covered headlessly in Tasks 3-4.)

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: FAIL at `MODE_STYLE has an extreme entry`.

- [ ] **Step 3: Implement**

`iterm/app.py`:

1. `MODE_STYLE` (~line 108): add `"extreme": ("✷", "EXTREME", DANGER),`.

2. `KEYBAR` line 1 (~line 129): append `("E×2", "extreme")` after
   `("t", "timers")`.

3. `help_text()` (~line 599): after the `Z Z` row add
   `row("E E", "EXTREME an INSANE session: auto-push a prompt while idle (double-press)")`,
   and in the ARM LEVELS block after `✦ INSANE` add
   `row("✷ EXTREME", "insane + pushes your prompt into an idle tab; budget-capped, gone on restart")`.

4. `BINDINGS` (~line 875, after the `Z` binding):
   `Binding("E", "extreme", "Extreme", show=True),`.

5. `__init__` flags (~line 916, after `_quit_armed`):

```python
        self._extreme_armed = False
        self._extreme_form = None    # None | {"sid": str}
```

6. Mode cell (~line 1059): after the `MODE_STYLE.get(...)` line:

```python
            if info.mode == "extreme":
                mlabel = f"{mlabel} {info.extreme_fires_left}"
```

7. New action + form methods, next to `action_zap` (~line 2007):

```python
    # --- extreme (E E): push-prompt an INSANE session while idle ---------
    def action_extreme(self) -> None:
        if self._any_overlay_open() or self._extreme_form is not None:
            return
        log = self.query_one(Log)
        sid = self._selected_sid()
        if not sid or not self.watcher or sid == self._own_sid:
            return
        info = self.watcher.sessions.get(sid)
        if info is None:
            return
        if info.mode not in ("insane", "extreme"):
            log.write_line(
                "extreme: requires INSANE first (SPACE cycles arm level)")
            return
        if not self._extreme_armed:
            self._extreme_armed = True
            self.set_timer(self._CONFIRM_WINDOW,
                           lambda: setattr(self, "_extreme_armed", False))
            log.write_line(
                f"extreme ARMED: press E again to configure the push "
                f"prompt (auto-cancels in {int(self._CONFIRM_WINDOW)}s)")
            return
        self._extreme_armed = False
        self._extreme_form_open(sid, info)

    def _extreme_form_open(self, sid, info) -> None:
        self._extreme_form = {"sid": sid}
        inp = Input(value=info.extreme_prompt,
                    placeholder="do the highest-value thing you can do "
                                "without me; don't wait for my review",
                    id="extreme_prompt")
        self.mount(inp)
        inp.focus()
        self.query_one(Log).write_line(
            f"EXTREME {info.title}: enter arms "
            f"{self.watcher.extreme_fires} push(es) - empty disarms - "
            f"esc cancels")

    def _extreme_form_close(self) -> None:
        self._extreme_form = None
        try:
            self.query_one("#extreme_prompt").remove()
        except Exception:
            pass

    def _extreme_form_save(self) -> None:
        if self._extreme_form is None:
            return
        try:
            text = self.query_one("#extreme_prompt").value.strip()
        except Exception:
            text = ""
        sid = self._extreme_form["sid"]
        log = self.query_one(Log)
        if not text:
            if self.watcher and self.watcher.clear_extreme(sid):
                log.write_line("extreme: disarmed - back to INSANE")
            self._extreme_form_close()
            return
        if self.watcher and self.watcher.set_extreme(sid, text):
            info = self.watcher.sessions.get(sid)
            log.write_line(
                f"EXTREME armed on {info.title if info else sid}: "
                f"{self.watcher.extreme_fires} push(es), then back to INSANE")
        else:
            log.write_line("extreme: could not arm (session gone or "
                           "not INSANE)")
        self._extreme_form_close()
```

8. `on_input_submitted` (~line 1713): extend to

```python
    def on_input_submitted(self, event) -> None:
        if self._timer_form is not None:
            self._timer_form_save()
        elif self._extreme_form is not None:
            self._extreme_form_save()
```

9. `action_dismiss_view` (search for `def action_dismiss_view`): at its very
   top, before any overlay handling, add

```python
        if self._extreme_form is not None:
            self._extreme_form_close()
            return
```

   (Escape while the form is up cancels JUST the form - mirrors the timer
   form's escape note at ~line 1720-1732. Also verify the refresh
   suppression at ~line 1232 keys off any focused Input, so the extreme
   Input inherits it; if it checks a specific id, generalize it.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_extreme.py`, then `python3 iterm/test_app.py`.
Expected: all `ALL PASSED` / exit 0.

- [ ] **Step 5: Manual smoke check (deferred to operator)**

Note in the final report: live `E E` -> form -> arm -> watch a push happen is
a human check, same as the other double-press keys.

- [ ] **Step 6: Commit**

```bash
git add iterm/app.py iterm/test_extreme.py
git commit -m "feat(tui): E E arms extreme - prompt form, budget in the mode cell"
```

---

### Task 6: Status bar, README, full suite

**Files:**
- Modify: `iterm/statusbar.py` (~line 24-40)
- Modify: `README.md` (the arm-modes / TUI keys sections; find them with
  `grep -n "insane" README.md`)
- Modify: `iterm/test_extreme.py`

**Interfaces:**
- Consumes: `statusbar.MODE_CIRCLE`, `statusbar.MODE_TEXT`,
  `statusbar.label`.
- Produces: display-only chrome; the badge NEVER sets extreme (no code path
  is added to the RPC - nothing to remove, just do not add one).

- [ ] **Step 1: Add failing tests**

Append to `iterm/test_extreme.py` (call from `__main__`):

```python
def test_statusbar_label():
    import statusbar
    chk("extreme circle is purple",
        statusbar.MODE_CIRCLE.get("extreme") == "\U0001f7e3")
    chk("extreme text on the badge",
        "RELAY:extreme" in statusbar.label("extreme"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_extreme.py`
Expected: FAIL at `extreme circle is purple`.

- [ ] **Step 3: Implement**

`iterm/statusbar.py`: `MODE_CIRCLE` gains `"extreme": "\U0001f7e3",  # purple
circle - pushing` (~line 29) and `MODE_TEXT` gains `"extreme": "extreme"`
(~line 39-40).

`README.md`: in the arm-levels documentation add one line for extreme
(insane + idle pushes; `E E` in the TUI; budget-capped via `extreme_fires`;
dwell via `extreme_dwell`; disarmed by a relay restart; a genuine question
is still never auto-answered), and add `E×2 extreme` wherever the TUI key
list enumerates `R×2 / W×2 / Z×2`.

- [ ] **Step 4: Run the FULL suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`.

- [ ] **Step 5: Commit**

```bash
git add iterm/statusbar.py README.md iterm/test_extreme.py
git commit -m "feat(statusbar): extreme badge; docs: extreme insane mode"
```
