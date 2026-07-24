# Session Timers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach one or more scheduled "timers" to any relay session, each firing a payload string into it on a 1-90 minute interval - cron-like behavior per session.

**Architecture:** A pure `iterm/timers.py` owns all scheduling decisions (due/firable/next-due/reconfirm); the swarm DB gains a `timers` table (source of truth); the watcher's existing 2s poll loop fires due timers through the same "inject when idle" path used for swarm delivery; the TUI gains a `t` overlay with relay's first text-input field. Config-file authoring is a DEFERRED extension (not in this plan). Spec: `docs/specs/2026-07-24-session-timers-design.md`.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `dataclasses`, `time`), Textual for the TUI overlay + `Input` widget. No new dependencies.

## Global Constraints

- NO em-dash characters (U+2014) anywhere. Plain `-` only. The glyphs `⏲ ● ○` and box-drawing characters in code ARE required - copy exactly.
- No pytest: test files are `iterm/test_*.py` with a `run()` function and a `__main__` block `sys.exit(0 if run() else 1)`, auto-globbed by `test/run.sh`. Helper: `def check(msg, cond)` prints `  OK  `/` FAIL ` and returns bool; accumulate with `ok &= check(...)`. Watcher/app tests use async fns run via `asyncio.run` with a local `chk(name, cond)`.
- `iterm/timers.py` imports NEITHER `iterm2` NOR `sqlite3` - pure logic, unit-tested standalone (like `gates.py`/`swarm.py`/`statusbar.py`/`titles.py`).
- Interval bounds: `1 <= interval_min <= 90` (clamp out-of-range).
- Timer modes are exactly `idle | now`.
- Timers NEVER auto-fire after a relay restart: they load `active=0` and require restore confirmation (unless `[timers] autostart = true`). Timers created/edited in a LIVE run are `active=1` immediately.
- Pause freezes all timers. Dry-run never injects (audits `would-fire`). Audit BEFORE the send (log-before-act), same as approvals/deliveries.
- Relay's own panel tab (`session_id == own_sid`) can never carry a timer.
- Firing is ALWAYS through the audited engine path; the UI never sends keystrokes for a timer directly (even "fire now" just backdates the clock).
- Commit after every task; short imperative subjects; no Co-Authored-By trailer.

## File Structure

```
iterm/timers.py        # CREATE pure: clamp/sanitize/is_due/due_timers/firable/next_due_in/needs_reconfirm
iterm/test_timers.py   # CREATE
iterm/db.py            # MODIFY timers table + CRUD (no file import)
iterm/config.py        # MODIFY [timers] require_armed + autostart + reconfirm_days
iterm/settings.py      # MODIFY two toggles + one number in SETTINGS
iterm/watcher.py       # MODIFY _fire_timers in poll loop; startup restore gate; pending set
iterm/app.py           # MODIFY 't' overlay (list + actions + add/edit Input); indicators; preview block
iterm/test_*.py        # MODIFY db/config/settings/watcher/app coverage
README.md              # MODIFY session-timers section + config keys + keymap
```

A "timer" flows as a dict-or-Row with these keys everywhere (sqlite3.Row and
test dicts both support `t["key"]`): `id, iterm_session_id, label,
interval_min, payload, mode, enabled, active, last_fired_at, bound_at,
created_at`.

---

### Task 1: timers.py - pure scheduling logic

**Files:**
- Create: `iterm/timers.py`
- Test: `iterm/test_timers.py`

**Interfaces:**
- Produces:
  - `clamp_interval(n) -> int` (1..90)
  - `sanitize_payload(s) -> str` (collapse newlines to spaces, trim)
  - `is_due(timer, now) -> bool` (enabled AND active AND `last_fired_at + interval_min*60 <= now`)
  - `due_timers(timers, now) -> list`
  - `firable(timer, *, ready, paused, armed, require_armed) -> bool`
  - `next_due_in(timer, now) -> float` (seconds; negative when overdue)
  - `needs_reconfirm(timer, now, reconfirm_days) -> bool` (`bound_at` older than the window; always False when `reconfirm_days <= 0`)
  - `MODES = ("idle", "now")`, `INTERVAL_MIN = 1`, `INTERVAL_MAX = 90`

- [ ] **Step 1: Write the failing test**

Create `iterm/test_timers.py`:

```python
"""Tests for the pure session-timer logic. No iTerm2/sqlite imports.

Run: python3 iterm/test_timers.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import timers  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _t(**kw):
    base = dict(enabled=1, active=1, interval_min=5, mode="idle",
                last_fired_at=1000.0, bound_at=1000.0)
    base.update(kw)
    return base


def run():
    ok = True

    ok &= check("clamp below -> 1", timers.clamp_interval(0) == 1)
    ok &= check("clamp above -> 90", timers.clamp_interval(999) == 90)
    ok &= check("clamp in range", timers.clamp_interval(37) == 37)
    ok &= check("clamp non-int -> 1", timers.clamp_interval("x") == 1)

    ok &= check("sanitize strips newlines",
                "\n" not in timers.sanitize_payload("a\nb\r\nc"))
    ok &= check("sanitize trims", timers.sanitize_payload("  hi  ") == "hi")

    ok &= check("due when interval elapsed",
                timers.is_due(_t(interval_min=5, last_fired_at=1000.0),
                              now=1000.0 + 5 * 60))
    ok &= check("not due before interval",
                not timers.is_due(_t(interval_min=5, last_fired_at=1000.0),
                                  now=1000.0 + 5 * 60 - 1))
    ok &= check("disabled never due",
                not timers.is_due(_t(enabled=0), now=10 ** 9))
    ok &= check("inactive never due",
                not timers.is_due(_t(active=0), now=10 ** 9))
    batch = [_t(interval_min=1, last_fired_at=0.0),
             _t(interval_min=1, last_fired_at=0.0, enabled=0),
             _t(interval_min=90, last_fired_at=0.0)]
    ok &= check("due_timers filters", len(timers.due_timers(batch, now=120.0)) == 1)

    ok &= check("idle mode needs ready",
                not timers.firable(_t(mode="idle"), ready=False, paused=False,
                                   armed=True, require_armed=False))
    ok &= check("idle fires when ready",
                timers.firable(_t(mode="idle"), ready=True, paused=False,
                               armed=False, require_armed=False))
    ok &= check("now mode ignores ready",
                timers.firable(_t(mode="now"), ready=False, paused=False,
                               armed=False, require_armed=False))
    ok &= check("pause freezes everything",
                not timers.firable(_t(mode="now"), ready=True, paused=True,
                                   armed=True, require_armed=False))
    ok &= check("require_armed blocks unarmed",
                not timers.firable(_t(mode="now"), ready=True, paused=False,
                                   armed=False, require_armed=True))
    ok &= check("require_armed allows armed",
                timers.firable(_t(mode="now"), ready=True, paused=False,
                               armed=True, require_armed=True))

    ok &= check("next_due_in counts down",
                timers.next_due_in(_t(interval_min=5, last_fired_at=1000.0),
                                   now=1000.0 + 60) == 4 * 60)

    day = 86400.0
    ok &= check("needs_reconfirm past the window",
                timers.needs_reconfirm(_t(bound_at=0.0), now=8 * day,
                                       reconfirm_days=7))
    ok &= check("no reconfirm within the window",
                not timers.needs_reconfirm(_t(bound_at=0.0), now=6 * day,
                                           reconfirm_days=7))
    ok &= check("reconfirm disabled at 0",
                not timers.needs_reconfirm(_t(bound_at=0.0), now=10 ** 9,
                                           reconfirm_days=0))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_timers.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'timers'`

- [ ] **Step 3: Write the implementation**

Create `iterm/timers.py`:

```python
"""Session timers - pure scheduling logic (no iterm2/sqlite imports).

A timer fires a payload string into a session every `interval_min` minutes. This
module decides WHEN (due), WHETHER (firable, given pause/arm/ready), and when a
binding is too old to trust (needs_reconfirm). The DB stores timers; the watcher
does the sending. Pure, so it is unit-testable standalone like gates.py.

A "timer" is any mapping with keys enabled/active/interval_min/mode/
last_fired_at/bound_at (sqlite3.Row and plain dict both work via t["key"]).
"""
from __future__ import annotations

INTERVAL_MIN = 1
INTERVAL_MAX = 90
MODES = ("idle", "now")
_DAY = 86400.0


def clamp_interval(n) -> int:
    """Coerce to an int minute count within [1, 90]. Junk -> 1."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return INTERVAL_MIN
    return max(INTERVAL_MIN, min(INTERVAL_MAX, v))


def sanitize_payload(s) -> str:
    """Single-line payload: any newline becomes a space, ends trimmed - so a
    payload can never carry an embedded Enter that submits early."""
    if not s:
        return ""
    return " ".join(str(s).split("\n")).replace("\r", " ").strip()


def is_due(timer, now) -> bool:
    if not (timer["enabled"] and timer["active"]):
        return False
    return (timer["last_fired_at"] or 0) + timer["interval_min"] * 60 <= now


def due_timers(timers, now) -> list:
    return [t for t in timers if is_due(t, now)]


def firable(timer, *, ready, paused, armed, require_armed) -> bool:
    """Fire gate for an already-due timer. Pause freezes all; require_armed
    blocks unarmed sessions; idle mode waits for a ready prompt, now does not."""
    if paused:
        return False
    if require_armed and not armed:
        return False
    if timer["mode"] == "idle" and not ready:
        return False
    return True


def next_due_in(timer, now) -> float:
    return (timer["last_fired_at"] or 0) + timer["interval_min"] * 60 - now


def needs_reconfirm(timer, now, reconfirm_days) -> bool:
    """True when the binding is older than the re-confirm window - a stale
    session_id (recycled UUID) guard. Disabled when reconfirm_days <= 0."""
    if not reconfirm_days or reconfirm_days <= 0:
        return False
    return now - (timer["bound_at"] or 0) > reconfirm_days * _DAY
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_timers.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 5: Commit**

```bash
git add iterm/timers.py iterm/test_timers.py
git commit -m "feat(timers): pure scheduling logic (due/firable/reconfirm)"
```

---

### Task 2: db.py - timers table + CRUD

**Files:**
- Modify: `iterm/db.py` (add to `_SCHEMA`, add functions)
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces:
  - `add_timer(conn, *, iterm_session_id, label, interval_min, payload, mode, active=1, now=None) -> int` (sets `bound_at = now`, `last_fired_at = now`)
  - `list_timers(conn, iterm_session_id) -> List[Row]`
  - `all_timers(conn) -> List[Row]`
  - `update_timer(conn, timer_id, **fields) -> None`
  - `set_timer_enabled(conn, timer_id, enabled) -> None`
  - `delete_timer(conn, timer_id) -> None`
  - `mark_timer_fired(conn, timer_id, now=None) -> None`
  - `restore_session_timers(conn, iterm_session_id, now=None) -> int` (active=1, last_fired=now, bound_at=now)
  - `deactivate_all_timers(conn) -> None`
  - `restore_all_present_timers(conn, present_sids, now=None) -> None`

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_db.py` inside `run()` (before the final `print`):

```python
    # --- session timers -------------------------------------------------------
    tid = db.add_timer(conn, iterm_session_id="SID1", label="api",
                       interval_min=5, payload="check PRs", mode="idle",
                       now=1000.0)
    rows = db.list_timers(conn, "SID1")
    ok &= check("add_timer + list_timers", len(rows) == 1
                and rows[0]["payload"] == "check PRs"
                and rows[0]["active"] == 1 and rows[0]["enabled"] == 1
                and rows[0]["bound_at"] == 1000.0)
    db.set_timer_enabled(conn, tid, False)
    ok &= check("set_timer_enabled off",
                db.list_timers(conn, "SID1")[0]["enabled"] == 0)
    db.mark_timer_fired(conn, tid, now=2000.0)
    ok &= check("mark_timer_fired sets last_fired_at",
                db.list_timers(conn, "SID1")[0]["last_fired_at"] == 2000.0)
    db.update_timer(conn, tid, interval_min=15, mode="now")
    r = db.list_timers(conn, "SID1")[0]
    ok &= check("update_timer", r["interval_min"] == 15 and r["mode"] == "now")

    db.deactivate_all_timers(conn)
    ok &= check("deactivate_all_timers",
                db.list_timers(conn, "SID1")[0]["active"] == 0)
    n = db.restore_session_timers(conn, "SID1", now=3000.0)
    rr = db.list_timers(conn, "SID1")[0]
    ok &= check("restore_session_timers activates + resets clock + rebinds",
                n == 1 and rr["active"] == 1 and rr["last_fired_at"] == 3000.0
                and rr["bound_at"] == 3000.0)

    db.add_timer(conn, iterm_session_id="SID2", label="b", interval_min=1,
                 payload="p", mode="now", now=1000.0)
    db.deactivate_all_timers(conn)
    db.restore_all_present_timers(conn, ["SID1", "SID2"], now=4000.0)
    ok &= check("restore_all_present_timers activates each present session",
                db.list_timers(conn, "SID1")[0]["active"] == 1
                and db.list_timers(conn, "SID2")[0]["active"] == 1)

    db.delete_timer(conn, tid)
    ok &= check("delete_timer", db.list_timers(conn, "SID1") == [])
    ok &= check("all_timers sees other sessions' timers",
                any(t["iterm_session_id"] == "SID2" for t in db.all_timers(conn)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_db.py`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'add_timer'`

- [ ] **Step 3: Add the schema**

Append this table to the `_SCHEMA` string in `iterm/db.py` (after the `tasks`
table, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS timers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  iterm_session_id TEXT,
  label TEXT NOT NULL DEFAULT '',
  interval_min INTEGER NOT NULL DEFAULT 5,
  payload TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'idle',
  enabled INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  last_fired_at REAL NOT NULL DEFAULT 0,
  bound_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT 0
);
```

`_SCHEMA` runs on every `connect()` via `CREATE TABLE IF NOT EXISTS`; a new
table needs no ALTER migration (same as `tasks`). Confirm by reading where
`_SCHEMA` executes in `connect()`.

- [ ] **Step 4: Add the CRUD functions**

Add near the other task helpers in `iterm/db.py` (`_now(now)` is the existing
helper returning `now` or `time.time()`):

```python
def add_timer(conn, *, iterm_session_id, label, interval_min, payload, mode,
              active=1, now=None) -> int:
    cur = conn.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, enabled, active, last_fired_at, bound_at, created_at) "
        "VALUES(?,?,?,?,?,1,?,?,?,?)",
        (iterm_session_id, label, int(interval_min), payload, mode,
         int(active), _now(now), _now(now), _now(now)))
    conn.commit()
    return cur.lastrowid


def list_timers(conn, iterm_session_id) -> list:
    return conn.execute(
        "SELECT * FROM timers WHERE iterm_session_id=? ORDER BY id",
        (iterm_session_id,)).fetchall()


def all_timers(conn) -> list:
    return conn.execute("SELECT * FROM timers ORDER BY id").fetchall()


def update_timer(conn, timer_id, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE timers SET {cols} WHERE id=?",
                 (*fields.values(), timer_id))
    conn.commit()


def set_timer_enabled(conn, timer_id, enabled) -> None:
    update_timer(conn, timer_id, enabled=int(bool(enabled)))


def delete_timer(conn, timer_id) -> None:
    conn.execute("DELETE FROM timers WHERE id=?", (timer_id,))
    conn.commit()


def mark_timer_fired(conn, timer_id, now=None) -> None:
    update_timer(conn, timer_id, last_fired_at=_now(now))


def restore_session_timers(conn, iterm_session_id, now=None) -> int:
    cur = conn.execute(
        "UPDATE timers SET active=1, last_fired_at=?, bound_at=? "
        "WHERE iterm_session_id=?",
        (_now(now), _now(now), iterm_session_id))
    conn.commit()
    return cur.rowcount


def deactivate_all_timers(conn) -> None:
    conn.execute("UPDATE timers SET active=0")
    conn.commit()


def restore_all_present_timers(conn, present_sids, now=None) -> None:
    for sid in present_sids:
        restore_session_timers(conn, sid, now=now)
```

- [ ] **Step 5: Run tests**

Run: `python3 iterm/test_db.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 6: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): timers table + CRUD + restore helpers"
```

---

### Task 3: config.py + settings.py - the [timers] settings

**Files:**
- Modify: `iterm/config.py`, `iterm/settings.py`
- Test: `iterm/test_config.py`, `iterm/test_settings.py`

**Interfaces:**
- Produces: `Config.timers_require_armed: bool = False`, `Config.timers_autostart: bool = False`, `Config.timers_reconfirm_days: float = 7.0`; INI `[timers] require_armed / autostart / reconfirm_days`.

- [ ] **Step 1: Write the failing config test**

Add to `iterm/test_config.py` (before the dump round-trip block):

```python
    # timers: two bools + a number; defaults; bad values warn
    cfg, _ = config.load("/nonexistent/relay-config")
    ok &= check("timers defaults",
                cfg.timers_require_armed is False
                and cfg.timers_autostart is False
                and cfg.timers_reconfirm_days == 7.0)
    p = _write("[timers]\nrequire_armed = true\nautostart = true\n"
               "reconfirm_days = 3\n")
    cfg, warns = config.load(p)
    ok &= check("timers keys parsed",
                cfg.timers_require_armed is True and cfg.timers_autostart is True
                and cfg.timers_reconfirm_days == 3.0 and warns == [])
    p = _write("[timers]\nrequire_armed = maybe\n")
    cfg, warns = config.load(p)
    ok &= check("bad timers bool -> false + warning",
                cfg.timers_require_armed is False
                and any("require_armed" in w for w in warns))
    p = _write("[timers]\nreconfirm_days = soon\n")
    cfg, warns = config.load(p)
    ok &= check("bad reconfirm_days -> default + warning",
                cfg.timers_reconfirm_days == 7.0
                and any("reconfirm_days" in w for w in warns))
```

And extend the `custom` round-trip dataclass replace call:

```python
        theme="amber", preview_panel=False,
        timers_require_armed=True, timers_autostart=True,
        timers_reconfirm_days=3.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_config.py`
Expected: FAIL (`Config` has no field `timers_require_armed`).

- [ ] **Step 3: Implement in config.py**

Add to the `Config` dataclass (after `preview_panel`):

```python
    timers_require_armed: bool = False
    timers_autostart: bool = False
    timers_reconfirm_days: float = 7.0
```

In `load()` (after the `preview` block, before the return), reuse the existing
`_get_float` helper for the number:

```python
    try:
        t_armed = cp.getboolean("timers", "require_armed",
                                fallback=d.timers_require_armed)
    except ValueError:
        warns.append("config: [timers] require_armed must be true/false - "
                     "using false")
        t_armed = False
    try:
        t_auto = cp.getboolean("timers", "autostart",
                               fallback=d.timers_autostart)
    except ValueError:
        warns.append("config: [timers] autostart must be true/false - "
                     "using false")
        t_auto = False
    t_recon = _get_float(cp, "timers", "reconfirm_days",
                         d.timers_reconfirm_days, warns)
```

Add to the `Config(...)` return kwargs:

```python
        timers_require_armed=t_armed,
        timers_autostart=t_auto,
        timers_reconfirm_days=t_recon,
```

Add to `dump()` (after the `[layout]` block):

```python
        "\n[timers]\n"
        f"require_armed  = {'true' if cfg.timers_require_armed else 'false'}\n"
        f"autostart      = {'true' if cfg.timers_autostart else 'false'}\n"
        f"reconfirm_days = {cfg.timers_reconfirm_days:g}\n"
```

Note: `_get_float`'s warning text includes the key name, so the
`reconfirm_days` warning test passes.

- [ ] **Step 4: Write the failing settings test**

Add to `iterm/test_settings.py`:

```python
    ok &= check("timers settings flip/step",
                settings.change(c, "timers_require_armed", +1).timers_require_armed
                is (not c.timers_require_armed)
                and settings.change(c, "timers_autostart", +1).timers_autostart
                is (not c.timers_autostart)
                and settings.change(c, "timers_reconfirm_days", +1).timers_reconfirm_days
                == c.timers_reconfirm_days + 1.0)
```

- [ ] **Step 5: Implement in settings.py**

Add to the `SETTINGS` list (new group after the BEHAVIOR rows):

```python
    ("TIMERS", "timers_require_armed", "toggle", None),
    ("TIMERS", "timers_autostart", "toggle", None),
    ("TIMERS", "timers_reconfirm_days", "number", (0.0, 1.0)),
```

These are read live by the engine / at startup, so no `_LIVE`/`_APP_LIVE` entry
is needed (the watcher reads `self.cfg` each tick). The editor will show a
`restart to apply` tag if changed - acceptable and honest (require_armed is
picked up live, but the tag does no harm; leave them non-live here).

- [ ] **Step 6: Run tests**

Run: `python3 iterm/test_config.py`, `python3 iterm/test_settings.py`, `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 7: Commit**

```bash
git add iterm/config.py iterm/settings.py iterm/test_config.py iterm/test_settings.py
git commit -m "feat(config): [timers] require_armed + autostart + reconfirm_days"
```

---

### Task 4: watcher.py - fire engine + startup restore gate

**Files:**
- Modify: `iterm/watcher.py`
- Test: `iterm/test_watcher.py`

**Interfaces:**
- Consumes: `timers.due_timers/firable/needs_reconfirm` (Task 1); `db.list_timers/mark_timer_fired/update_timer/deactivate_all_timers/restore_all_present_timers/all_timers` (Task 2); `self.cfg.timers_require_armed/timers_autostart/timers_reconfirm_days` (Task 3).
- Produces:
  - `Watcher._fire_timers(info)` (async, per session per tick after `_check_stale`)
  - `Watcher._load_timers_on_start()` (once in the poll loop; deactivate-all restore gate unless autostart; fills `pending_timer_sids`)
  - `Watcher.pending_timer_sids` (set) - the app reads it for the `⏲?` indicator.

- [ ] **Step 1: Write the failing tests**

Add a `timer_tests()` async fn to `iterm/test_watcher.py` (uses the existing
`FakeSession` with `async_send_text` -> `self.sent`, a temp DB via the module's
`RELAY_DB`/hermetic setup, and monkeypatched `W.notify_mac`/`W.audit.record`):

```python
async def timer_tests():
    """Watcher fires due timers: now fires immediately, idle waits for ready,
    pause freezes, require_armed gates, past-reconfirm deactivates."""
    from watcher import Watcher, SessionInfo
    import db as D
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    W.notify_mac = lambda *a, **k: None
    W.audit.record = lambda *a, **k: True

    conn = D.connect()
    D.add_timer(conn, iterm_session_id="s1", label="api", interval_min=1,
                payload="run lint", mode="now", now=0.0)
    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    fs = FakeSession()
    info = SessionInfo("s1", title="api", _iterm_session=fs, mode="safe",
                       state="working")
    w.sessions["s1"] = info
    await w._fire_timers(info)
    chk("now-mode fires immediately (busy ok)",
        any("run lint" in s for s in fs.sent))

    D.add_timer(conn, iterm_session_id="s2", label="w", interval_min=1,
                payload="check PRs", mode="idle", now=0.0)
    w2 = Watcher(connection=None, dry_run=False, cfg=C.Config())
    fs2 = FakeSession()
    info2 = SessionInfo("s2", title="w", _iterm_session=fs2, mode="safe",
                        state="working")
    w2.sessions["s2"] = info2
    await w2._fire_timers(info2)
    chk("idle-mode waits while busy", fs2.sent == [])
    info2.state = "idle"
    info2.last_screen = ["│ > ", "? for shortcuts"]
    await w2._fire_timers(info2)
    chk("idle-mode fires at a ready prompt",
        any("check PRs" in s for s in fs2.sent))

    D.add_timer(conn, iterm_session_id="s3", label="w", interval_min=1,
                payload="x", mode="now", now=0.0)
    w3 = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w3.paused = True
    fs3 = FakeSession()
    info3 = SessionInfo("s3", title="w", _iterm_session=fs3, mode="safe")
    w3.sessions["s3"] = info3
    await w3._fire_timers(info3)
    chk("pause freezes timers", fs3.sent == [])

    D.add_timer(conn, iterm_session_id="s4", label="w", interval_min=1,
                payload="y", mode="now", now=0.0)
    w4 = Watcher(connection=None, dry_run=False,
                 cfg=C.Config(timers_require_armed=True))
    fs4 = FakeSession()
    info4 = SessionInfo("s4", title="w", _iterm_session=fs4, mode="off")
    w4.sessions["s4"] = info4
    await w4._fire_timers(info4)
    chk("require_armed blocks an unarmed session", fs4.sent == [])

    # a binding older than reconfirm_days deactivates instead of firing
    tid5 = D.add_timer(conn, iterm_session_id="s5", label="w", interval_min=1,
                       payload="z", mode="now", now=0.0)   # bound_at = 0
    w5 = Watcher(connection=None, dry_run=False,
                 cfg=C.Config(timers_reconfirm_days=7))
    fs5 = FakeSession()
    info5 = SessionInfo("s5", title="w", _iterm_session=fs5, mode="safe")
    w5.sessions["s5"] = info5
    await w5._fire_timers(info5)     # now() >> 7 days after bound_at=0
    chk("past-reconfirm timer does not fire",
        fs5.sent == []
        and D.list_timers(conn, "s5")[0]["active"] == 0
        and "s5" in w5.pending_timer_sids)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

Register in `__main__`:

```python
    r_timer = asyncio.run(timer_tests())
```
and include `r_timer` in the final `sys.exit(0 if (... and r_timer) else 1)`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL (`Watcher` has no `_fire_timers`).

- [ ] **Step 3: Implement the engine**

Add near the top of `iterm/watcher.py` (with the other imports):

```python
import timers as timers_mod
```

Add `pending_timer_sids` init and a load flag in `__init__` (with the other
swarm state):

```python
        self.pending_timer_sids: set = set()
        self._timers_loaded = False
```

Add `_fire_timers` to the `Watcher` class (after `_deliver`):

```python
    async def _fire_timers(self, info: SessionInfo) -> None:
        """Fire at most one due, firable timer for this session per tick. now
        mode injects immediately; idle waits for a ready Claude prompt. Pause
        freezes; require_armed gates on arm level; dry-run would-fire. A binding
        older than reconfirm_days is deactivated (back to pending) instead of
        firing - the stale-session-id guard. Audit BEFORE the send. Best-effort:
        DB/iTerm2 errors are logged, never break the loop."""
        if info.session_id == self.own_sid:
            return
        s = info._iterm_session
        if s is None:
            return
        try:
            conn = self._swarm_conn()
            rows = swarmdb.list_timers(conn, info.session_id)
        except Exception as e:
            self._note(f"timers db error: {e}")
            return
        now = time.time()
        due = timers_mod.due_timers(rows, now)
        if not due:
            return
        ready = (info.state == "idle"
                 and swarm.claude_prompt_ready(info.last_screen))
        armed = info.mode in ("safe", "wild", "insane")
        require_armed = getattr(self.cfg, "timers_require_armed", False)
        reconfirm = getattr(self.cfg, "timers_reconfirm_days", 7.0)
        for t in due:
            if timers_mod.needs_reconfirm(t, now, reconfirm):
                swarmdb.update_timer(conn, t["id"], active=0)
                self.pending_timer_sids.add(info.session_id)
                self._note(f"timer {t['id']} binding stale - re-confirm via t")
                continue
            if not timers_mod.firable(t, ready=ready, paused=self.paused,
                                      armed=armed, require_armed=require_armed):
                continue
            if self.dry_run:
                audit.record("would-fire", info.title, t["payload"][:500],
                             f"timer {t['id']}")
                self._note(f"DRY-RUN would fire timer -> {info.title}: "
                           f"{t['payload'][:60]}")
                swarmdb.mark_timer_fired(conn, t["id"], now=now)
                return
            if not audit.record("timer-fired", info.title, t["payload"][:500],
                                f"timer {t['id']}"):
                now2 = time.time()
                if now2 - info._last_notify_ts >= self.notify_cooldown:
                    info._last_notify_ts = now2
                    self._note(f"AUDIT-FAIL: not firing timer {t['id']}")
                return
            await s.async_send_text(t["payload"])
            await asyncio.sleep(0.3)
            await s.async_send_text("\r")
            swarmdb.mark_timer_fired(conn, t["id"], now=now)
            self._note(f"TIMER -> {info.title}: {t['payload'][:60]}")
            return    # one per tick
```

- [ ] **Step 4: Startup restore gate**

Add `_load_timers_on_start` to the class:

```python
    def _load_timers_on_start(self) -> None:
        """Restore gate: unless [timers] autostart, every saved timer starts
        inactive and its session is flagged pending (the app prompts to restore
        via the t overlay). Never raises."""
        try:
            conn = self._swarm_conn()
            if getattr(self.cfg, "timers_autostart", False):
                swarmdb.restore_all_present_timers(conn, list(self.sessions))
                self.pending_timer_sids = set()
            else:
                swarmdb.deactivate_all_timers(conn)
                self.pending_timer_sids = {
                    row["iterm_session_id"]
                    for row in swarmdb.all_timers(conn)
                    if row["iterm_session_id"] in self.sessions}
        except Exception as e:
            self._note(f"timers load error: {e}")
```

- [ ] **Step 5: Wire into the poll loop**

In `start()`'s loop, after the first `self._swarm_refresh_registry()` (so live
sessions exist), before the per-session block:

```python
            if not self._timers_loaded:
                self._timers_loaded = True
                self._load_timers_on_start()
```

In the per-session block, right after `await self._apply_title(info)` (or after
`self._check_stale(info)` if title code is absent):

```python
                        await self._fire_timers(info)
```

- [ ] **Step 6: Run tests**

Run: `python3 iterm/test_watcher.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 7: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "feat(watcher): fire due session timers; startup restore gate + stale-binding guard"
```

---

### Task 5: app.py - the `t` overlay (list + actions)

**Files:**
- Modify: `iterm/app.py`
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `db.list_timers/set_timer_enabled/delete_timer/mark_timer_fired/restore_session_timers` (Task 2); `timers.next_due_in` (Task 1); `watcher.pending_timer_sids` (Task 4).
- Produces: `action_timers()` bound to `t`; `#timersview` overlay; `self._timers_visible`, `self._timers_cursor`; pure `timers_view_text(rows, now, session_title, width)`; `_render_timers()`; `_swarm_db_conn()`.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_app.py`'s `go()`:

```python
    # --- timers overlay -------------------------------------------------------
    _tv = appmod.timers_view_text(
        [{"id": 1, "interval_min": 5, "payload": "check PRs", "mode": "idle",
          "enabled": 1, "active": 1, "last_fired_at": 1000.0}],
        now=1000.0, session_title="api", width=80)
    chk("timers_view_text lists interval + payload",
        "every 5m" in _tv and "check PRs" in _tv)
    chk("help advertises timers", "timers" in appmod.help_text().lower())

    to = _TestApp(_one(), dry_run=True)
    async with to.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        chk("t opens timers overlay",
            to._timers_visible
            and str(to.query_one("#timersview").styles.display) == "block")
        await pilot.press("t")
        await pilot.pause()
        chk("t closes it", not to._timers_visible)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("esc also closes timers overlay", not to._timers_visible)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL (`timers_view_text` / `#timersview` missing).

- [ ] **Step 3: Add the pure renderer**

Add near `audit_view_text` in `iterm/app.py`:

```python
def timers_view_text(rows, now, session_title, width) -> str:
    """The `t` overlay body: one line per timer for the selected session, plus
    countdown + on/off. Plain text (pane renders markup-free)."""
    import timers as _timers
    w = max(40, width)
    bar = "═" * w
    head = (f"╔{bar}╗\n"
            f" ⏲ TIMERS // {session_title[:w - 14]}\n"
            f" a add · enter edit · space on/off · g fire now · x delete · "
            f"r restore · t/esc close\n"
            f"╚{bar}╝\n")
    if not rows:
        return head + ("\n no timers on this session.\n\n"
                       " press a to add one: an interval (1-90 min) and a\n"
                       " payload string sent to this session on that schedule.")
    lines = []
    for r in rows:
        onoff = "● on " if r["enabled"] else "○ off"
        if not r["active"]:
            when = "needs restore (r)"
        else:
            secs = max(0, _timers.next_due_in(r, now))
            when = f"next in {int(secs) // 60}m{int(secs) % 60:02d}s"
        lines.append(f"  every {r['interval_min']}m  {r['mode']:<4} {onoff}  "
                     f"{when:<18} {str(r['payload'])[:max(10, w - 40)]}")
    return head + "\n".join(lines)
```

- [ ] **Step 4: Overlay wiring**

In `compose()`, add (next to `#helpview`/`#settingsview`):

```python
            yield Static("", id="timersview")
```

Append `#timersview` to the existing `#swarmview, #helpview, #settingsview` CSS
rule (shares the hidden-by-default + padding styling).

In `__init__`:

```python
        self._timers_visible = False
        self._timers_cursor = 0
```

Add the binding (next to `f`/`v`):

```python
        Binding("t", "timers", "Timers", show=False),
```

Extend `_any_overlay_open` to include `self._timers_visible`.

Add the helper + actions:

```python
    def _swarm_db_conn(self):
        if self._swarm_db is None:
            self._swarm_db = swarmdb.connect()
        return self._swarm_db

    def action_timers(self) -> None:
        if self._swarm_visible:
            self.action_swarm_view()
        if self._settings_visible:
            self.action_settings()
        if self._help_visible:
            self.action_help()
        sid = self._selected_sid()
        if sid == self._own_sid:
            self.query_one(Log).write_line(
                "timers: relay never fires into its own panel tab")
            return
        self._timers_visible = not self._timers_visible
        on = self._timers_visible
        self.query_one("#middle").styles.display = "none" if on else "block"
        self.query_one("#log").styles.display = "none" if on else "block"
        self.query_one("#timersview").styles.display = "block" if on else "none"
        if on:
            self._timers_cursor = 0
            self._render_timers()

    def _render_timers(self) -> None:
        sid = self._selected_sid()
        if not sid or not self.watcher:
            self.query_one("#timersview", Static).update("\n  no session.")
            return
        try:
            rows = [dict(r) for r in swarmdb.list_timers(
                self._swarm_db_conn(), sid)]
        except Exception as e:
            self.query_one("#timersview", Static).update(f"\n  db error: {e}")
            return
        info = self.watcher.sessions.get(sid)
        title = info.title if info else sid
        w = self.query_one("#timersview").size.width - 4
        self.query_one("#timersview", Static).update(
            timers_view_text(rows, time.time(), title, max(40, w)))
```

Add `#timersview` to `action_dismiss_view` (ESC):

```python
        elif self._timers_visible:
            self.action_timers()
```

Add overlay-local keys via `on_key` (context-sensitive; must coexist with the
Input added in Task 6, hence not Bindings):

```python
    def on_key(self, event) -> None:
        if not self._timers_visible:
            return
        sid = self._selected_sid()
        rows = [dict(r) for r in swarmdb.list_timers(self._swarm_db_conn(), sid)] \
            if sid else []
        cur = min(self._timers_cursor, max(0, len(rows) - 1))
        k = event.key
        if k == "up" and rows:
            self._timers_cursor = max(0, cur - 1); self._render_timers(); event.stop()
        elif k == "down" and rows:
            self._timers_cursor = min(len(rows) - 1, cur + 1); self._render_timers(); event.stop()
        elif k == "space" and rows:
            swarmdb.set_timer_enabled(self._swarm_db_conn(), rows[cur]["id"],
                                      not rows[cur]["enabled"])
            self._render_timers(); event.stop()
        elif k == "g" and rows:
            swarmdb.mark_timer_fired(self._swarm_db_conn(), rows[cur]["id"],
                                     now=time.time() - rows[cur]["interval_min"] * 60)
            self._render_timers(); event.stop()      # due next tick, audited
        elif k == "x" and rows:
            swarmdb.delete_timer(self._swarm_db_conn(), rows[cur]["id"])
            self._timers_cursor = 0; self._render_timers(); event.stop()
        elif k == "r" and sid:
            swarmdb.restore_session_timers(self._swarm_db_conn(), sid)
            if self.watcher:
                self.watcher.pending_timer_sids.discard(sid)
            self._render_timers(); event.stop()
        elif k in ("left", "right") and rows:
            import timers as _timers
            step = -1 if k == "left" else 1
            swarmdb.update_timer(self._swarm_db_conn(), rows[cur]["id"],
                                 interval_min=_timers.clamp_interval(
                                     rows[cur]["interval_min"] + step))
            self._render_timers(); event.stop()
        elif k == "m" and rows:
            swarmdb.update_timer(self._swarm_db_conn(), rows[cur]["id"],
                                 mode="now" if rows[cur]["mode"] == "idle" else "idle")
            self._render_timers(); event.stop()
```

Design note: interval (`left`/`right`) and mode (`m`) are edited DIRECTLY on the
selected list row - never inside a text field. This deliberately avoids the
conflict where a focused `Input` would eat `left`/`right` for its own cursor.
The `Input` in Task 6 is payload-ONLY. Update the overlay's key-hint line in
`timers_view_text` to reflect this:

```python
            f" a add · enter edit payload · left/right interval · m mode · "
            f"space on/off · g fire · x del · r restore · esc close\n"
```

(`g` "fire now" backdates `last_fired_at` so the audited engine path fires it -
the UI never sends the keystrokes itself.)

- [ ] **Step 5: help + keybar**

In `help_text()`, add after the `f` row:

```python
        row("t", "timers: schedule payloads to fire into this session (cron-like)"),
```

Add `("t", "timers")` to KEYBAR line 1 after `("f", "feed")`.

- [ ] **Step 6: Run tests**

Run: `python3 iterm/test_app.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 7: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): timers overlay (t) - list, toggle, fire-now, delete, restore"
```

---

### Task 6: app.py - the add/edit flow (payload text input)

**Files:**
- Modify: `iterm/app.py`
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `db.add_timer/update_timer` (Task 2); `timers.clamp_interval/sanitize_payload/MODES` (Task 1).
- Produces: `self._timer_form` state; `_timer_form_open/_timer_form_close/_timer_form_save`; a Textual `Input` (`#timer_payload`) mounted in `#timersview`.

- [ ] **Step 1: Write the failing test**

Add inside the timers pilot block in `iterm/test_app.py`:

```python
        await pilot.press("t")            # reopen
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        chk("a opens the add form", to._timer_form is not None)
        to.query_one("#timer_payload").value = "check PRs"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        import db as _db
        rows = _db.list_timers(to._swarm_db_conn(), to._selected_sid())
        chk("saved timer with typed payload + sane defaults",
            any(r["payload"] == "check PRs" and 1 <= r["interval_min"] <= 90
                for r in rows))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL (`_timer_form` / `#timer_payload` missing).

- [ ] **Step 3: Implement the form**

Add `Input` to the Textual widget imports:

```python
from textual.widgets import DataTable, Static, Log, Input
```

In `__init__`:

```python
        self._timer_form = None   # None | {"id": None|int, "interval": int, "mode": str}
```

Add the form methods:

```python
    def _timer_form_open(self, existing=None) -> None:
        self._timer_form = {
            "id": existing["id"] if existing else None,
            "interval": existing["interval_min"] if existing else 5,
            "mode": existing["mode"] if existing else "idle"}
        inp = Input(value=existing["payload"] if existing else "",
                    placeholder="payload (script name or a note to Claude)",
                    id="timer_payload")
        self.query_one("#timersview").mount(inp)
        inp.focus()
        self._render_timers()

    def _timer_form_close(self) -> None:
        self._timer_form = None
        try:
            self.query_one("#timer_payload").remove()
        except Exception:
            pass
        self._render_timers()

    def _timer_form_save(self) -> None:
        import timers as _timers
        if self._timer_form is None:
            return
        try:
            payload = _timers.sanitize_payload(
                self.query_one("#timer_payload").value)
        except Exception:
            payload = ""
        if not payload:
            self._timer_form_close()
            return
        sid = self._selected_sid()
        info = self.watcher.sessions.get(sid) if self.watcher else None
        interval = _timers.clamp_interval(self._timer_form["interval"])
        mode = self._timer_form["mode"]
        if self._timer_form["id"] is None:
            swarmdb.add_timer(self._swarm_db_conn(), iterm_session_id=sid,
                              label=info.title if info else sid,
                              interval_min=interval, payload=payload, mode=mode)
        else:
            swarmdb.update_timer(self._swarm_db_conn(), self._timer_form["id"],
                                 interval_min=interval, payload=payload,
                                 mode=mode)
        self._timer_form_close()
```

The `Input` is payload-ONLY. Interval/mode are edited in list mode (Task 5:
`left`/`right`/`m`), so the focused Input never needs those keys. Enter is
consumed by the Input (posts `Input.Submitted`), so saving goes through
`on_input_submitted`, not `on_key`; escape bubbles to `on_key` for cancel.

Add this block at the TOP of `on_key` (right after the
`if not self._timers_visible: return` guard):

```python
        if self._timer_form is not None:
            if event.key == "escape":
                self._timer_form_close(); event.stop()
            return    # every other key belongs to the focused payload Input
```

Add the submit handler (saves on Enter in the Input):

```python
    def on_input_submitted(self, event) -> None:
        if self._timer_form is not None:
            self._timer_form_save()
```

And add, in the no-form branch of `on_key` (alongside the space/g/x/r/left/right/m
handlers from Task 5):

```python
        elif k == "a":
            self._timer_form_open(); event.stop()
        elif k in ("enter", "e") and rows:
            self._timer_form_open(rows[cur]); event.stop()
```

Show a form banner in `_render_timers`: build `body` (the `timers_view_text`
output) then append a form line when a form is open, and `update` once:

```python
        body = timers_view_text(rows, time.time(), title, max(40, w))
        if self._timer_form is not None:
            f = self._timer_form
            verb = "EDIT" if f["id"] else "NEW"
            body += (f"\n\n  {verb} timer  (interval {f['interval']}m · mode "
                     f"{f['mode']} - adjust in the list) · type the payload "
                     f"below · enter save · esc cancel")
        self.query_one("#timersview", Static).update(body)
```

(A NEW timer is created with defaults interval=5 / mode=idle; change them on the
list row afterward with `left`/`right`/`m`. EDIT preserves the row's current
interval/mode - `_timer_form_open(existing)` reads them fresh from the row.)

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_app.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): timers add/edit form with payload text input"
```

---

### Task 7: Visibility (list indicator + preview block), README, sweep

**Files:**
- Modify: `iterm/app.py`, `README.md`
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `db.list_timers` (Task 2), `watcher.pending_timer_sids` (Task 4).
- Produces: pure `timer_badge(active, pending) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_app.py`:

```python
    chk("timer badge: active count wins, pending flag, else empty",
        appmod.timer_badge(active=2, pending=False) == "⏲2"
        and appmod.timer_badge(active=0, pending=True) == "⏲?"
        and appmod.timer_badge(active=0, pending=False) == "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL (`timer_badge` missing).

- [ ] **Step 3: Implement the indicator + preview block**

Add the pure helper to `iterm/app.py`:

```python
def timer_badge(active, pending) -> str:
    """Row indicator: ⏲N for N active timers, ⏲? when timers await restore,
    else empty. active wins."""
    if active:
        return f"⏲{active}"
    if pending:
        return "⏲?"
    return ""
```

In the row builder `add()` inside `_refresh`, after `title` is computed and
before `add_row`, append the badge to the title cell (do NOT add a column):

```python
            pend = info.session_id in getattr(self.watcher, "pending_timer_sids", set())
            act = 0
            try:
                act = sum(1 for t in swarmdb.list_timers(
                    self._swarm_db_conn(), info.session_id)
                    if t["active"] and t["enabled"])
            except Exception:
                pass
            badge = timer_badge(act, pend)
            if badge:
                title = f"{title} [{DIM}]{badge}[/]"
```

In `_update_preview`, in the normal-session branch after the header block, append
a compact TIMERS section (at most 4 lines + overflow note):

```python
        try:
            trows = [dict(r) for r in swarmdb.list_timers(
                self._swarm_db_conn(), sid)]
        except Exception:
            trows = []
        if trows:
            import timers as _timers
            tl = [" TIMERS"]
            for r in trows[:4]:
                st = "on" if r["enabled"] and r["active"] else (
                    "restore?" if not r["active"] else "off")
                secs = max(0, _timers.next_due_in(r, time.time()))
                tl.append(f"   every {r['interval_min']}m {r['mode']} [{st}] "
                          f"in {int(secs)//60}m: {str(r['payload'])[:w-24]}")
            if len(trows) > 4:
                tl.append(f"   (+{len(trows) - 4} more)")
            body = body + "\n" + "\n".join(tl)
```

(Ensure this is inserted where `body` is assembled for the normal-session
preview, before `preview.update(header + body)`.)

- [ ] **Step 4: README**

Add a `### Session timers` subsection under Configuration: what they are, the
`t` overlay + its keys, `idle`/`now` modes, the 1-90 interval, the
restore-on-start prompt (`⏲?` + `t` then `r`), and the config keys:

```ini
[timers]
require_armed  = false   ; only fire on an armed session
autostart      = false   ; skip the restore prompt; activate saved timers on start
reconfirm_days = 7        ; re-confirm a timer binding older than this (0 = never)
```

Update the project layout tree with `iterm/timers.py` + `iterm/test_timers.py`.
Add `t` to the key list. NO em-dash. (Do NOT document a `~/.relay/timers` file -
config-file authoring is a deferred extension, not in this build.)

- [ ] **Step 5: Final sweep**

```bash
./test/run.sh
grep -rn $'\u2014' iterm/ README.md docs/specs/2026-07-24-session-timers-design.md || echo "no em-dashes"
python3 - <<'EOF'
import sys; sys.path.insert(0, "iterm")
import timers
print(timers.clamp_interval(200), timers.sanitize_payload("a\nb"))
EOF
```

Expected: suite green, "no em-dashes", `90 a b`.

Live checks (HUMAN, deferred - list in the final report, do not run): create a
1-minute `now` timer on a scratch tab with payload `echo hi`, watch it fire;
create a 1-minute `idle` timer with a Claude nudge, confirm it waits for the
prompt; quit + relaunch relay, confirm the timer needs restore (`⏲?`), restore
it via `t` -> `r`.

- [ ] **Step 6: Commit**

```bash
git add iterm/app.py README.md
git commit -m "feat(tui): timer indicators + preview block; docs for session timers"
```

---

## Notes for the implementer

- **`on_key` vs Bindings:** the timers overlay uses `on_key` because it needs
  context-sensitive keys (list nav, form steering) that only apply while the
  overlay/form is open and must coexist with a focused `Input`. Always
  `event.stop()` after handling so the key does not also hit a global binding.
  While the `Input` is focused it consumes character keys; only the non-character
  keys (arrows/enter/escape) reach `on_key`.
- **One DB connection in the app:** reuse `self._swarm_db` via `_swarm_db_conn()`;
  never open a fresh connection per render.
- **The engine reads `self.cfg` live:** `require_armed` and `reconfirm_days` take
  effect without a restart because `_fire_timers` reads `self.cfg` each tick.
- **Firing is always audited:** the UI never sends keystrokes for a timer; even
  "fire now" backdates `last_fired_at` so the audited `_fire_timers` path sends.
- **Mounting the payload `Input`:** relay's overlays are bare `Static`s. A
  `Static` renders its own text and is not a reliable parent for an interactive
  child. If mounting the `Input` directly into `#timersview` misrenders, make
  `#timersview` a container: `Vertical(Static(id="timerslist"), id="timersview")`
  - render the list text into `#timerslist` (update `_render_timers` accordingly)
  and mount/remove the `Input` as a sibling in `#timersview`. Verify visually in
  the piloted test (the test only checks `to.query_one("#timer_payload")` exists
  and its value saves, so either structure passes the test - confirm the live
  look during the Task 7 human checks).
- **Focus + key routing:** while the payload `Input` has focus it consumes
  character keys, `enter` (-> `Input.Submitted` -> `on_input_submitted`), and
  `left`/`right` (its own cursor). `escape` bubbles to `App.on_key` for cancel.
  That is why interval/mode live on the list row (Task 5), not in the form. When
  no `Input` is focused and an overlay is open, keys reach `on_key`; unhandled
  keys (e.g. `t`, `escape` in list mode) fall through to the global Bindings
  (`action_timers` close / `action_dismiss_view`) - do NOT `event.stop()` those.
- **Deferred:** the `~/.relay/timers` config-file importer (name/title
  resolution, upsert/prune, a `source` partition) is intentionally NOT in this
  plan; it is additive when built.
```
