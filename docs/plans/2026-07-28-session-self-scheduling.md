# Session Self-Scheduling (`relay timer`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `relay timer add|list|rm` CLI verb group so a Claude session can register its own capped, idle-mode relay timer from inside its own iTerm2 tab.

**Architecture:** This is an authoring surface only. `iterm/timers.py` (the pure scheduling engine), `iterm/watcher.py` (the fire path), and `iterm/app.py` (the `t` overlay) are **not modified** - CLI-created rows are ordinary `timers` rows. Three guards live entirely in the CLI verb: forced `idle` mode, mandatory fire cap, and `--key` upsert. One DB migration adds a `key` column so re-registration is idempotent.

**Tech Stack:** Python 3 stdlib only (`argparse`, `sqlite3`, `re`). No third-party deps. Tests are hand-rolled `check()`-style suites with a `__main__` runner - **there is no pytest in this repo.**

**Spec:** `docs/specs/2026-07-28-session-self-scheduling-design.md`

## Global Constraints

- **No em-dash characters (U+2014) anywhere** - source, comments, docs, commit messages. Use a plain ASCII hyphen `-`.
- **No new dependencies.** Python 3 stdlib only.
- **Do not modify** `iterm/timers.py`, `iterm/watcher.py`, or `iterm/app.py`. If a task seems to require it, stop and flag it - that means the design was wrong.
- **Tests are not pytest.** Each suite is a module with `def run()` returning a bool, a `check(msg, cond)` helper, and `if __name__ == "__main__": sys.exit(0 if run() else 1)`. Add cases inside the existing `run()` function of the relevant file.
- **Full suite command:** `./test/run.sh`. Single suite: `python3 iterm/test_cli.py`.
- **Commit style:** `feat(cli):` / `feat(db):` / `docs:` prefixes, lowercase summary, matching recent history. No `Co-Authored-By` trailer.
- Interval clamp is `[1, 90]` (`timers.INTERVAL_MIN` / `INTERVAL_MAX`). Fire cap clamp for this CLI path is `[1, 50]`.

## File Structure

| File | Responsibility |
| --- | --- |
| `iterm/db.py` | MODIFY: `key` column in `_SCHEMA` timers block, migration `6`, `_CURRENT_VERSION` 6->7, new `get_timer_by_key()` |
| `iterm/cli.py` | MODIFY: `_TIMER_KEY_RE`, `cmd_timer_add`, `cmd_timer_list`, `cmd_timer_rm`, parser wiring |
| `iterm/test_db.py` | MODIFY: migration + `get_timer_by_key` coverage |
| `iterm/test_cli.py` | MODIFY: guard, upsert, list, rm coverage |
| `skills/relay-self-scheduling/SKILL.md` | CREATE: the judgment layer |
| `skills/relay-cli-reference.md` | MODIFY: `relay timer` verb docs |
| `skills/relay-worker/SKILL.md` | MODIFY: one-line cross-reference |
| `skills/relay-coordinator/SKILL.md` | MODIFY: one-line cross-reference |
| `README.md` | MODIFY: self-scheduling section |

Task order: DB first (CLI depends on the column), then CLI, then docs/skill.

---

### Task 1: `key` column + `get_timer_by_key`

**Files:**
- Modify: `iterm/db.py` (the `timers` block inside `_SCHEMA` around line 67; `_CURRENT_VERSION` and `_MIGRATIONS` around lines 110-127; the `# --- session timers ---` section around line 378)
- Test: `iterm/test_db.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `timers.key` column, `TEXT NOT NULL DEFAULT ''`
  - `db.get_timer_by_key(conn, iterm_session_id, key) -> Optional[sqlite3.Row]` - returns the row for that session+key, or `None`. Never matches an empty key (overlay-authored rows all have `key = ''` and must not collide).
  - `db.add_timer(...)` gains a keyword-only `key: str = ""` parameter, written into the new column.

**Background:** `db.py` versions the schema with `PRAGMA user_version`. Fresh DBs get the current shape straight from `_SCHEMA` and skip migrations entirely; existing DBs step through `_MIGRATIONS[v]` one version at a time. `_migrate` swallows `sqlite3.OperationalError` so re-applying an `ALTER` that already landed is harmless. You must update **both** `_SCHEMA` and `_MIGRATIONS` or fresh and existing DBs will diverge.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_db.py`, inside the existing `run()` function, near the other timer checks:

```python
    # --- self-scheduling: key column + lookup ---------------------------
    t_keyed = db.add_timer(conn, iterm_session_id="KEY-SID", label="self:prs",
                           interval_min=20, payload="check PRs", mode="idle",
                           max_fires=10, key="prs")
    row = db.get_timer_by_key(conn, "KEY-SID", "prs")
    ok &= check("get_timer_by_key finds the keyed timer",
                row is not None and row["id"] == t_keyed
                and row["key"] == "prs")

    ok &= check("get_timer_by_key misses a different key",
                db.get_timer_by_key(conn, "KEY-SID", "nope") is None)

    ok &= check("get_timer_by_key is scoped to the session",
                db.get_timer_by_key(conn, "OTHER-SID", "prs") is None)

    # Overlay-authored rows carry key='' and must never be matched by lookup,
    # or every operator-created timer would collide with the next one.
    db.add_timer(conn, iterm_session_id="KEY-SID", label="operator",
                 interval_min=5, payload="ping", mode="now")
    ok &= check("empty key never matches",
                db.get_timer_by_key(conn, "KEY-SID", "") is None)

    ok &= check("add_timer defaults key to empty string",
                db.list_timers(conn, "KEY-SID")[1]["key"] == "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_db.py`
Expected: FAIL - `AttributeError: module 'db' has no attribute 'get_timer_by_key'` (the suite raises rather than printing FAIL, which is fine; the point is it does not pass).

- [ ] **Step 3: Add the column to `_SCHEMA`**

In `iterm/db.py`, in the `CREATE TABLE IF NOT EXISTS timers(...)` block, add the column immediately after `fire_count`:

```sql
  fire_count INTEGER NOT NULL DEFAULT 0,
  key TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0
);
```

- [ ] **Step 4: Add the migration and bump the version**

Change `_CURRENT_VERSION = 6` to `_CURRENT_VERSION = 7`, and add key `6` to `_MIGRATIONS` after the existing key `5` entry:

```python
    # v7: self-scheduling. CLI-created timers carry a stable per-session `key`
    # so a session re-registering the same responsibility upserts instead of
    # stacking duplicates. Overlay-created rows keep key='' and are never
    # matched by get_timer_by_key.
    6: ("ALTER TABLE timers ADD COLUMN key TEXT NOT NULL DEFAULT ''",),
```

- [ ] **Step 5: Add `key` to `add_timer` and write `get_timer_by_key`**

In `iterm/db.py`, replace the existing `add_timer` with:

```python
def add_timer(conn, *, iterm_session_id, label, interval_min, payload, mode,
              active=1, max_fires=10, key="", now: Optional[float] = None) -> int:
    cur = conn.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, enabled, active, last_fired_at, bound_at, max_fires, "
        "fire_count, key, created_at) VALUES(?,?,?,?,?,1,?,?,?,?,0,?,?)",
        (iterm_session_id, label, int(interval_min), payload, mode,
         int(active), _now(now), _now(now), int(max_fires), key, _now(now)))
    conn.commit()
    return cur.lastrowid
```

Then add, directly after `list_timers`:

```python
def get_timer_by_key(conn, iterm_session_id, key) -> Optional[sqlite3.Row]:
    """The one CLI-created timer for this session under this key, or None.

    An empty key never matches: overlay-created timers all carry key='' and
    must not be treated as the same timer as each other.
    """
    if not key:
        return None
    return conn.execute(
        "SELECT * FROM timers WHERE iterm_session_id=? AND key=?",
        (iterm_session_id, key)).fetchone()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 iterm/test_db.py`
Expected: PASS, ending `ALL PASS`.

- [ ] **Step 7: Run the full suite (nothing else may regress)**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`. `add_timer` gained a keyword-only parameter with a default, so every existing caller (`app.py:1660`, the test suites) still works unchanged.

- [ ] **Step 8: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): timers.key column + get_timer_by_key for CLI upsert

Migration 6 (user_version 6 -> 7) adds a stable per-session key so a
self-registering session upserts its timer instead of stacking duplicates.
Overlay-created rows keep key='' and are never matched by the lookup."
```

---

### Task 2: `relay timer add`

**Files:**
- Modify: `iterm/cli.py` (verb handlers section after `cmd_task_list` around line 309; parser section after the `task` subparsers around line 936)
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.get_timer_by_key(conn, sid, key)`, `db.add_timer(..., key=...)`, `db.update_timer(conn, timer_id, **fields)` from Task 1 and existing code.
- Produces:
  - `cli._TIMER_KEY_RE` - compiled `^[a-z][a-z0-9_-]{0,23}$`
  - `cli.cmd_timer_add(args) -> int`
  - CLI: `relay timer add --key K --every N --times M --say TEXT`

**Background:** Every session-facing verb resolves the tab through `cli.my_iterm_id()`, which strips the `w0t2p0:` prefix off `$ITERM_SESSION_ID`. Do **not** use `_require_me` here - that demands a swarm registration (`relay register`), and per spec §2 timers bind to a tab, not a swarm name. The primary use case is an unregistered lone Claude tab.

`_err(msg)` prints `relay: <msg>` to stderr and returns `1`. All rejections use it.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_cli.py`, inside `run()`, after the existing task-board checks. Note `run_cli` accepts `iterm_id=` to switch identity:

```python
    # --- self-scheduling: relay timer add -------------------------------
    code, out, err = run_cli("timer", "add", "--key", "pr-duty",
                             "--every", "20", "--times", "10",
                             "--say", "Read .relay/prompts/pr-duty.md and "
                                      "do what it says.",
                             iterm_id="w0t1p0:TIMER-SID")
    trows = db.list_timers(db.connect(), "TIMER-SID")
    ok &= check("timer add creates one row", code == 0 and len(trows) == 1)
    ok &= check("timer add forces idle mode", trows[0]["mode"] == "idle")
    ok &= check("timer add stores interval + cap",
                trows[0]["interval_min"] == 20 and trows[0]["max_fires"] == 10)
    ok &= check("timer add labels the row self:<key>",
                trows[0]["label"] == "self:pr-duty")
    ok &= check("timer add is live immediately",
                trows[0]["enabled"] == 1 and trows[0]["active"] == 1)

    # Upsert: same key updates in place, never stacks.
    code, _, _ = run_cli("timer", "add", "--key", "pr-duty",
                         "--every", "30", "--times", "5", "--say", "new text",
                         iterm_id="w0t1p0:TIMER-SID")
    trows = db.list_timers(db.connect(), "TIMER-SID")
    ok &= check("timer add upserts by key (still one row)",
                code == 0 and len(trows) == 1)
    ok &= check("timer add upsert applies new values",
                trows[0]["interval_min"] == 30 and trows[0]["payload"] == "new text"
                and trows[0]["max_fires"] == 5)

    # Interval clamps to [1, 90]; cap clamps to [1, 50]. Both bounds.
    run_cli("timer", "add", "--key", "clamped", "--every", "999",
            "--times", "999", "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "clamped")
    ok &= check("timer add clamps interval to 90 and cap to 50",
                r["interval_min"] == 90 and r["max_fires"] == 50)

    run_cli("timer", "add", "--key", "lowclamp", "--every", "0",
            "--times", "1", "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "lowclamp")
    ok &= check("timer add clamps interval up to the 1m floor",
                r["interval_min"] == 1 and r["max_fires"] == 1)

    run_cli("timer", "add", "--key", "junk", "--every", "abc",
            "--times", "5", "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "junk")
    ok &= check("timer add survives a junk interval (clamp_interval -> 1)",
                r is not None and r["interval_min"] == 1)

    # Guards. Missing flags must produce OUR message, not argparse's, so the
    # session learns why the flag exists.
    code, _, err = run_cli("timer", "add", "--every", "20", "--times", "5",
                           "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("missing --key gives the teaching error",
                code == 1 and "--key" in err and "instead of" in err)

    code, _, err = run_cli("timer", "add", "--key", "nocap", "--every", "20",
                           "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("missing --times gives the teaching error",
                code == 1 and "--times" in err)

    code, _, err = run_cli("timer", "add", "--key", "capless", "--every", "20",
                           "--times", "0", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects --times 0", code == 1 and "cap" in err.lower())

    code, _, err = run_cli("timer", "add", "--key", "BadKey!", "--every", "20",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects a malformed key", code == 1 and "key" in err.lower())

    code, _, err = run_cli("timer", "add", "--key", "empty", "--every", "20",
                           "--times", "5", "--say", "   ",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects an empty payload", code == 1)

    # Newlines are collapsed so a payload can never submit early.
    run_cli("timer", "add", "--key", "multi", "--every", "20", "--times", "5",
            "--say", "line one\nline two", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "multi")
    ok &= check("timer add sanitizes newlines out of the payload",
                "\n" not in r["payload"] and r["payload"] == "line one line two")

    # A long inline payload warns but still succeeds.
    code, out, err = run_cli("timer", "add", "--key", "longish", "--every", "20",
                             "--times", "5", "--say", "y" * 250,
                             iterm_id="w0t1p0:TIMER-SID")
    ok &= check("long inline payload warns but succeeds",
                code == 0 and ".relay/prompts" in (out + err))

    # No iTerm identity at all -> clean error, not a traceback.
    code, _, err = run_cli("timer", "add", "--key", "k", "--every", "5",
                           "--times", "5", "--say", "x", iterm_id="")
    ok &= check("timer add without ITERM_SESSION_ID errors cleanly",
                code == 1 and "ITERM_SESSION_ID" in err)
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse rejects the unknown `timer` subcommand, so the first check reports FAIL.

- [ ] **Step 3: Add the key regex and the handler**

In `iterm/cli.py`, next to the existing `_KIND_RE` definition, add:

```python
# Self-scheduling timer key: one short lowercase slug, stable across
# re-registrations so a session upserts its timer instead of stacking copies.
_TIMER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

# Inline payloads longer than this get a nudge toward a prompt file. Not an
# error: a long one-liner still works, it just ages badly across compaction.
_PAYLOAD_WARN_LEN = 200
```

Then add the handler after `cmd_task_list`:

```python
def cmd_timer_add(args) -> int:
    """Register (or update) a timer bound to THIS tab.

    Deliberately does NOT use _require_me: timers bind to an iTerm session id,
    not to a swarm name, so this must work in a plain unregistered Claude tab.
    Three guards live here and nowhere else - the engine treats these rows as
    ordinary timers:
      - mode is always 'idle' ('now' would inject mid-turn into our own tab)
      - the fire cap is mandatory (unattended self-injection needs a ceiling)
      - --key upserts (stops the fire -> re-register -> duplicate cascade)
    """
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")

    key = (args.key or "").strip()
    if not _TIMER_KEY_RE.match(key):
        return _err("--key must be a short slug: lowercase letter first, then "
                    "letters/digits/-/_ (max 24), e.g. --key pr-duty. It is "
                    "what makes re-registering update the timer instead of "
                    "adding another one.")

    payload = timers.sanitize_payload(args.say)
    if not payload:
        return _err("--say cannot be empty")

    try:
        times = int(args.times)
    except (TypeError, ValueError):
        times = 0
    if times < 1:
        return _err("--times must be at least 1 - a self-registered timer "
                    "always needs a fire cap. Try --times 10.")
    times = min(50, times)

    interval = timers.clamp_interval(args.every)
    conn = db.connect()
    existing = db.get_timer_by_key(conn, sid, key)
    now = time.time()
    if existing is not None:
        db.update_timer(conn, existing["id"], interval_min=interval,
                        payload=payload, mode="idle", max_fires=times,
                        fire_count=0, enabled=1, active=1,
                        last_fired_at=now, bound_at=now)
        tid = existing["id"]
        verb = "updated"
    else:
        tid = db.add_timer(conn, iterm_session_id=sid, label=f"self:{key}",
                           interval_min=interval, payload=payload,
                           mode="idle", max_fires=times, key=key, now=now)
        verb = "registered"

    print(f"timer {tid} {verb}: '{key}' every {interval}m, "
          f"{times} fire(s), first in {interval}m")
    if len(payload) > _PAYLOAD_WARN_LEN:
        print("note: that payload is long. Prefer writing the instructions to "
              ".relay/prompts/<key>.md and using a short pointer payload - it "
              "survives context compaction and stays editable.")
    return 0
```

- [ ] **Step 4: Import `timers` and wire the parser**

At the top of `iterm/cli.py`, next to `import db` / `import swarm`, add:

```python
import timers  # noqa: E402
```

Then in `build_parser()`, after the `task` subparser block and before the `spawn` block, add:

```python
    tm = sub.add_parser("timer", help="timers bound to THIS session")
    tmsub = tm.add_subparsers(dest="timer_cmd", required=True)

    tma = tmsub.add_parser("add", help="register/update a timer on this tab")
    # --key/--times/--say are deliberately NOT argparse-required: the handler
    # rejects them with a message that explains WHY they exist (the duplicate
    # cascade, the mandatory cap), which argparse's terse "the following
    # arguments are required" cannot. --every has no such lesson, and a missing
    # --every would silently clamp to 1 minute, so it stays required here.
    tma.add_argument("--key",
                     help="stable slug, e.g. pr-duty; re-running with the same "
                          "key updates that timer instead of adding another")
    tma.add_argument("--every", required=True,
                     help="interval in minutes (clamped to 1-90)")
    tma.add_argument("--times",
                     help="fire cap, 1-50 - mandatory; there is no unlimited")
    tma.add_argument("--say",
                     help="the single-line text to inject when it fires")
    tma.set_defaults(fn=cmd_timer_add)
```

Verify the teaching errors reach the user rather than argparse's terse version:

```bash
python3 -c "import sys; sys.path.insert(0,'iterm'); import cli; cli.main(['timer','add','--every','20','--times','5','--say','x'])"
```
Expected on stderr: the `--key must be a short slug ...` message, not `the following arguments are required: --key`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_cli.py`
Expected: PASS, ending `ALL PASS`.

- [ ] **Step 6: Confirm `--mode now` is not reachable**

Run: `python3 -c "import sys; sys.path.insert(0,'iterm'); import cli; sys.exit(cli.main(['timer','add','--key','k','--every','5','--times','5','--say','x','--mode','now']))"`
Expected: non-zero exit with argparse's `unrecognized arguments: --mode now`. The flag is absent by design, which is the enforcement - `now` stays operator-only in the `t` overlay.

- [ ] **Step 7: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py
git commit -m "feat(cli): relay timer add - a session registers its own capped timer

Bound to \$ITERM_SESSION_ID, not to a swarm name, so it works in a plain
unregistered Claude tab. Three CLI-side guards: forced idle mode, mandatory
1-50 fire cap, and --key upsert so re-registering never stacks duplicates.
The engine is untouched - these are ordinary timer rows."
```

---

### Task 3: `relay timer list` and `relay timer rm`

**Files:**
- Modify: `iterm/cli.py` (after `cmd_timer_add`; parser additions inside the `timer` subparser block from Task 2)
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `cli._TIMER_KEY_RE` and the `timer` subparser from Task 2; `db.list_timers`, `db.get_timer_by_key`, `db.delete_timer`.
- Produces: `cli.cmd_timer_list(args) -> int`, `cli.cmd_timer_rm(args) -> int`.

**Background:** Both verbs are scoped to `my_iterm_id()`, so one tab can never see or delete another tab's timers. `db.delete_timer(conn, timer_id)` takes a raw id, so `rm --id` must first confirm the row belongs to this session - otherwise a guessed integer would delete a neighbour's timer.

`cli._ago(ts)` already exists for humanised timestamps; the countdown here is a *future* delta so format it inline rather than reusing `_ago`.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_cli.py`, inside `run()`, after the Task 2 checks:

```python
    # --- self-scheduling: list + rm -------------------------------------
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer list shows this session's timers",
                code == 0 and "pr-duty" in out and "clamped" in out)

    # A different tab sees none of them.
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:STRANGER-SID")
    ok &= check("timer list is scoped to the calling session",
                code == 0 and "pr-duty" not in out)

    # rm by key.
    code, _, _ = run_cli("timer", "rm", "--key", "clamped",
                         iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm --key deletes it",
                code == 0
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "clamped") is None)

    # rm by id, from the wrong session, must refuse.
    victim = db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty")
    code, _, err = run_cli("timer", "rm", "--id", str(victim["id"]),
                           iterm_id="w0t1p0:STRANGER-SID")
    ok &= check("timer rm cannot touch another session's timer",
                code == 1
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty") is not None)

    # rm of something that isn't there.
    code, _, err = run_cli("timer", "rm", "--key", "ghost",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm on a missing key errors cleanly", code == 1)

    # rm by id, from the owning session, works.
    code, _, _ = run_cli("timer", "rm", "--id", str(victim["id"]),
                         iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm --id works for the owner",
                code == 0
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty") is None)

    # Empty list is a friendly message, not a crash.
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:EMPTY-SID")
    ok &= check("timer list with no timers says so",
                code == 0 and "no timers" in out.lower())
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse rejects the unknown `list` / `rm` subcommands of `timer`.

- [ ] **Step 3: Write the two handlers**

In `iterm/cli.py`, after `cmd_timer_add`:

```python
def _timer_line(t, now: float) -> str:
    """One rendered row for `relay timer list`."""
    left = t["max_fires"] - t["fire_count"] if t["max_fires"] > 0 else None
    due = (t["last_fired_at"] or 0) + t["interval_min"] * 60 - now
    when = "due now" if due <= 0 else f"in {int(due // 60)}m{int(due % 60):02d}s"
    state = "on" if (t["enabled"] and t["active"]) else "off"
    fires = "unlimited" if left is None else f"{left} left"
    key = t["key"] or "-"
    return (f"  {t['id']:>3}  {key:<24}  every {t['interval_min']:>2}m  "
            f"{state:<3}  {fires:<11}  next {when:<10}  {t['payload'][:60]}")


def cmd_timer_list(args) -> int:
    """List only THIS session's timers - a tab can never see another's."""
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    rows = db.list_timers(db.connect(), sid)
    if not rows:
        print("no timers on this session")
        return 0
    now = time.time()
    print(f"  {'id':>3}  {'key':<24}  {'interval':<9}  {'st':<3}  "
          f"{'fires':<11}  {'next':<15}  payload")
    for t in rows:
        print(_timer_line(t, now))
    return 0


def cmd_timer_rm(args) -> int:
    """Delete one of THIS session's timers, by key or by id.

    The id path re-checks ownership: db.delete_timer takes a raw id, so without
    the check a guessed integer would delete another tab's timer.
    """
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    conn = db.connect()
    if args.key:
        row = db.get_timer_by_key(conn, sid, args.key.strip())
        if row is None:
            return _err(f"no timer with key '{args.key}' on this session")
    elif args.id:
        row = next((t for t in db.list_timers(conn, sid)
                    if str(t["id"]) == str(args.id)), None)
        if row is None:
            return _err(f"no timer {args.id} on this session")
    else:
        return _err("give --key <slug> or --id <n> (see: relay timer list)")
    db.delete_timer(conn, row["id"])
    print(f"timer {row['id']} removed")
    return 0
```

- [ ] **Step 4: Wire the parsers**

In `build_parser()`, inside the `timer` block added in Task 2, after `tma.set_defaults(...)`:

```python
    tml = tmsub.add_parser("list", help="list this session's timers")
    tml.set_defaults(fn=cmd_timer_list)

    tmr = tmsub.add_parser("rm", help="remove one of this session's timers")
    tmr.add_argument("--key", help="the timer's key")
    tmr.add_argument("--id", help="the timer's numeric id (see: timer list)")
    tmr.set_defaults(fn=cmd_timer_rm)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_cli.py`
Expected: PASS, ending `ALL PASS`.

- [ ] **Step 6: Run the full suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`.

- [ ] **Step 7: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py
git commit -m "feat(cli): relay timer list + rm, scoped to the calling session

Both resolve the tab from \$ITERM_SESSION_ID, so one session can never see or
delete another's timers. rm --id re-checks ownership before delete_timer,
which takes a raw id."
```

---

### Task 4: The `relay-self-scheduling` skill and the docs

**Files:**
- Create: `skills/relay-self-scheduling/SKILL.md`
- Modify: `skills/relay-cli-reference.md`, `skills/relay-worker/SKILL.md`, `skills/relay-coordinator/SKILL.md`, `README.md`

**Interfaces:**
- Consumes: the CLI surface from Tasks 2-3 (`relay timer add|list|rm` with exactly the flags defined there).
- Produces: no code.

**Background:** The division of labour from spec §6 is the point of this task. **Mechanics live in the CLI** (guardrails fire whether or not a skill loaded); **the skill carries only judgment the CLI cannot check**. Do not restate flag syntax in the skill - that belongs in `relay-cli-reference.md`, which both existing skills already point at.

Read `skills/relay-worker/SKILL.md` first to match its voice and heading style before writing.

- [ ] **Step 1: Create the skill**

Create `skills/relay-self-scheduling/SKILL.md`:

````markdown
---
name: relay-self-scheduling
description: Use when asked to take standing responsibility for something on an interval ("you're responsible for PRs", "check X every N minutes", "register a timer in relay") - writes a durable prompt file and registers a capped relay timer bound to this tab
---

# Relay Self-Scheduling

You can register a timer that injects text back into **your own tab** on an
interval. Flags and exact syntax: `skills/relay-cli-reference.md`, or
`relay timer add --help`. This skill covers only the judgment the CLI cannot
check for you.

## First: should this be a timer at all?

Most "keep an eye on X" requests should NOT become a relay timer.

| Situation | Use instead |
| --- | --- |
| One follow-up at a known time | Just do it now, or say when you will |
| You need to poll something inside a single turn | A loop in the turn |
| The work belongs to a different session | `relay send` - queued, not scheduled |
| The interval is "whenever I next feel like it" | Nothing. Ask. |

A relay timer earns its place when **all** of these hold:
- The responsibility is standing, not a one-off.
- It must run when nobody is watching the terminal.
- Each firing is useful even if nothing has changed since the last one.

If any of those is false, say so and do not register one.

## Write the prompt file first, then register a pointer

Never put the real instructions in `--say`. Two reasons, both fatal:

1. Payloads are single-line - embedded newlines are collapsed at save time, so
   a good multi-paragraph prompt becomes one unreadable line.
2. By the third firing your context has likely been compacted. A payload that
   assumes you remember this conversation will not work; a pointer to a
   self-contained file will.

So:

```
1. Write .relay/prompts/<key>.md - self-contained. Write it for a reader who
   has never seen this conversation: what to check, where, what counts as
   done, what to do if there is nothing to do.
2. relay timer add --key <key> --every 20 --times 10 \
     --say "Read .relay/prompts/<key>.md and do what it says."
```

Worked example, "you are responsible for PRs":

```bash
# .relay/prompts/pr-duty.md holds the actual duty description
relay timer add --key pr-duty --every 20 --times 10 \
  --say "Read .relay/prompts/pr-duty.md and do what it says."
```

## Interval and cap sanity

The CLI clamps to 1-90 minutes and 1-50 fires. Both clamps permit choices you
should not make.

- **Interval:** 15-30 minutes for review/monitoring duties. Under 10 minutes
  means you fire before the previous turn's work has landed. A 1-minute
  self-firing timer is pathological - the clamp allows it, do not use it.
- **Cap:** pick the number that covers the session you are actually in.
  `--times 10` at 20-minute intervals is a bounded ~3-hour shift. When it runs
  out, the human is back and can re-register. That is the design, not a
  limitation - there is no unlimited on this path.

Multiply before you commit: `--every 2 --times 50` is over an hour and a half
of near-continuous unattended token burn.

## Tell the human what you did

After registering, state the key, interval, cap, and total wall-clock span in
one line. They can kill it with `x` in relay's `t` overlay, and they should
know it exists before they walk away.

## Clean up

When the responsibility ends, remove it:

```bash
relay timer list                 # this session's timers only
relay timer rm --key pr-duty
```

## Two things you cannot do

- **Schedule into another session.** Timers bind to your own tab. Use
  `relay send` to reach another session.
- **Use `now` mode.** It injects mid-turn, which would corrupt your own turn.
  It is operator-only in the `t` overlay, and the flag does not exist on the
  CLI.
````

- [ ] **Step 2: Document the verbs in the CLI reference**

`skills/relay-cli-reference.md` is a flat list of verbs, each as a 4-space-indented usage line followed by an 8-space-indented prose block. Insert this block after the `relay task list` entry and before the `relay spawn` entry, matching that formatting exactly:

```
    relay timer add --key <slug> --every <1-90> --times <1-50> --say "<text>"
        Register a timer on YOUR OWN tab: every <every> minutes, when you are
        idle at a ready prompt, <say> is typed into you and submitted. Unlike
        the other verbs this needs no `relay register` - timers bind to the tab,
        not to a swarm name, so it works in a plain unregistered Claude session.
        --key is a stable slug: re-running add with the same key UPDATES that
        timer rather than adding a second one. --times is a mandatory fire cap
        (1-50, no unlimited) - when it runs out the timer stops and a human can
        re-register it. Mode is always idle; there is no --mode flag, because
        firing mid-turn would corrupt your own turn. Payload is single line
        (newlines are flattened), so put real instructions in a file and make
        the payload a pointer to it - see the relay-self-scheduling skill.

    relay timer list
        Your own timers only: id, key, interval, on/off, fires left, next-fire
        countdown, payload. You cannot see another session's timers.

    relay timer rm --key <slug> | --id <n>
        Remove one of your own timers. --id only works for a timer on your tab.
```

- [ ] **Step 3: Cross-reference from the two existing skills**

Add one line near the end of both `skills/relay-worker/SKILL.md` and `skills/relay-coordinator/SKILL.md`:

```markdown
**Standing interval duties:** if you are asked to own something on a repeating
schedule ("check PRs every 20 minutes"), use the `relay-self-scheduling` skill -
do not hand-roll a loop.
```

- [ ] **Step 4: Add the README section**

Read the existing session-timers section of `README.md` and add a self-scheduling subsection after it, in the same voice. Cover: what it is, the worked `pr-duty` example, the three guards and why each exists, and the fact that self-registered timers show as `self:<key>` in the `t` overlay and do not survive a relay restart unattended (the restart gate loads every timer `active = 0`).

- [ ] **Step 5: Verify the skill's frontmatter parses**

Run: `python3 -c "
import sys
p='skills/relay-self-scheduling/SKILL.md'
t=open(p).read()
assert t.startswith('---\n'), 'missing frontmatter'
fm=t.split('---',2)[1]
assert 'name: relay-self-scheduling' in fm, 'bad name'
assert 'description:' in fm, 'missing description'
print('frontmatter OK')
"`
Expected: `frontmatter OK`.

- [ ] **Step 6: Check for em-dashes across everything touched**

Run:
```bash
python3 - <<'EOF'
import sys
files = ["README.md", "skills/relay-self-scheduling/SKILL.md",
         "skills/relay-cli-reference.md", "skills/relay-worker/SKILL.md",
         "skills/relay-coordinator/SKILL.md", "iterm/cli.py", "iterm/db.py"]
bad = False
for f in files:
    for i, line in enumerate(open(f, encoding="utf-8"), 1):
        if "—" in line or "–" in line:
            print(f"{f}:{i}: {line.rstrip()}")
            bad = True
print("EM-DASHES FOUND - replace with plain ASCII hyphens" if bad else "clean")
sys.exit(1 if bad else 0)
EOF
```
Expected: `clean`, exit 0.

- [ ] **Step 7: Run the full suite one last time**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`.

- [ ] **Step 8: Commit**

```bash
git add skills/ README.md
git commit -m "docs(skill): relay-self-scheduling - judgment layer for session timers

Standalone rather than a relay-worker section: relay-worker gates on being
told you are a swarm worker, but the primary case is a lone Claude tab told
'you own PRs'. Mechanics stay in the CLI where they fire regardless; the
skill covers when NOT to schedule, writing a payload that survives
compaction, and interval/cap sanity."
```

---

## Manual verification (after all four tasks)

The automated suites cover the CLI in-process against a temp DB. This confirms the end-to-end path in a real relay.

- [ ] Start relay in one iTerm2 tab; open a plain Claude tab (do **not** `relay register` it - unregistered is the primary case).
- [ ] In the Claude tab, ask it to take a standing duty. It should load `relay-self-scheduling`, write `.relay/prompts/<key>.md`, and run `relay timer add`.
- [ ] In relay's panel, press `t` on that session. The timer appears with label `self:<key>`, mode `idle`, and a countdown.
- [ ] Wait for a fire (or press `g` to fire now). Confirm the payload lands **only** when the session is idle at a ready prompt, never mid-turn.
- [ ] Press `p` to pause relay. Confirm the countdown freezes and nothing fires.
- [ ] Restart relay. Confirm the timer comes back as **pending restore** (`⏲?`), not firing - this is the restart gate that makes "live immediately" safe.
- [ ] Re-run the same `relay timer add --key <same key>` from the session. Confirm the `t` overlay still shows exactly one row, with updated values.
