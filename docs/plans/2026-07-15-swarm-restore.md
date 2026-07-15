# Swarm Restore + Clean Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user resume abandoned swarm work: persist each session's workdir + mission, detect dead/closed sessions, and add `relay restore` (respawn dead workers in their own dir to finish their tasks) and `relay clean` (reset abandoned tasks to unowned todo and remove ghost rows).

**Architecture:** Three DB columns on `sessions` (schema v4: workdir, spawn_prompt, closed_at). The watcher stamps `closed_at` when a tab vanishes (debounced, sync-gated). Pure planning functions in `swarm.py` build restore/clean plans from rows; two CLI verbs in `cli.py` print the plan, confirm, and act (restore reuses `spawn_worker`). Spec: `docs/specs/2026-07-15-swarm-restore-design.md`.

**Tech Stack:** Python 3 stdlib (`sqlite3`, `argparse`), existing `iterm2`. No new deps.

## Global Constraints

- NO em-dash (U+2014) anywhere. Plain `-` only. The glyphs `◉ ▲ ✦ ‼ ⊘ ⧗` in code are required; copy exactly.
- No pytest: `iterm/test_*.py` with `run()`/`__main__` runners (exit 0/1), auto-globbed by `test/run.sh`.
- `db.py` and `swarm.py` import neither `iterm2` nor (for swarm.py) `sqlite3`.
- Schema migration uses the existing step ladder in `db.py` (`_CURRENT_VERSION`, `_MIGRATIONS`, `_migrate`). New version is **4**.
- `restore` and `clean` both print a plan and confirm (stdin `[y/N]`) unless `--yes`; `--dry-run` prints the plan and stops.
- restore/clean act only on **closed** sessions (auto) or **named** sessions (manual restore); never a live session, never a `done` task.
- `clean` sets abandoned non-done tasks to `state='todo', owner=NULL`, deletes the closed session rows, and drops undelivered messages addressed to deleted sessions.
- closed_at marking: 2 consecutive misses AND the tick's roster sync succeeded.
- Commit after every task; short imperative subjects; no Co-Authored-By trailer.

## Reference: codebase facts

- `iterm/db.py`: migration ladder is `_CURRENT_VERSION = 3`, `_MIGRATIONS = {1: (...,), 2: (...,)}`, `_migrate(conn)` walks `v -> _CURRENT_VERSION` running each step's SQL (wrapped in try/except OperationalError). `sessions` columns today: name, iterm_session_id, role, project, status_text, registered_at, last_seen, arm_request, mode. `register(conn, name, iterm_session_id, role, project="", now=None)` upserts (ON CONFLICT keeps registered_at, preserves project when the new project is ""). `list_sessions(conn, project=None)`, `list_tasks(conn, project=None, owner=None)`, `set_task_state(conn, id, state, now=None)`, `get_session(conn, name)`, `undelivered(conn, to_name=None)`, `get_task(conn, id)`.
- `iterm/spawn.py`: `async def spawn_worker(name, project, prompt, workdir, role="worker", arm="off") -> str` creates a tab, sets its name, `db.register(...)`, optional `db.set_arm_request`, cd+claude, sends the first prompt. `first_prompt(name, project, prompt, role="worker")` returns the skill-invoking prompt. `BOOT_DELAY` from env.
- `iterm/watcher.py`: `start()` loop each tick does `app = async_get_app`; `await self._sync_sessions(app)` (wrapped in try/except that `_note`s "roster sync error"); then `self._swarm_refresh_registry()`; `self._check_gone()`. `self.sessions` is the dict of LIVE tabs keyed by bare sid (populated/pruned by `_sync_sessions`). `_swarm_refresh_registry` iterates `swarmdb.list_sessions(conn)` building `self.registry` keyed by iterm_session_id. `self._swarm_conn()` returns the lazy sqlite conn. `_note(msg)` logs.
- `iterm/cli.py`: verbs are argparse subparsers in `build_parser()`; each handler `cmd_*(args) -> int` (0 ok, 1 user error via `_err`, 2 argparse). `_require_me(conn)` resolves the caller. `cmd_doctor` already reads db read-only. `main(argv)` dispatches.
- `bin/relay`: dispatch `case` line lists verbs; `-h` uses `sed -n '2,18p'`.
- Test idioms: `iterm/test_db.py` builds temp DBs via `_tmpdb()`; `iterm/test_cli.py` sets `RELAY_DB`+`ITERM_SESSION_ID` before importing `cli`, drives `cli.main` via `run_cli(*argv, iterm_id=...)` capturing (code, stdout, stderr); `iterm/test_swarm.py` tests pure functions; `iterm/test_watcher.py` uses `FakeSession` and monkeypatches `W.swarmdb.*`.

## File structure

```
iterm/db.py       # MODIFY: schema v4 cols; set_session_context; closed_at
                  #   helpers; closed_sessions; reset_owner_tasks;
                  #   delete_session; prune_messages
iterm/swarm.py    # MODIFY: restore/clean plan builders (pure, over rows)
iterm/spawn.py    # MODIFY: record workdir+spawn_prompt on the session
iterm/watcher.py  # MODIFY: closed_at marking (debounced, sync-gated);
                  #   orphan_count
iterm/cli.py      # MODIFY: cmd_restore, cmd_clean; doctor orphans line;
                  #   spawn records context; register --dir
iterm/app.py      # MODIFY: orphan hint in subtitle; R key -> shell out
bin/relay         # MODIFY: dispatch restore|clean
README.md         # MODIFY: restore/clean docs
iterm/test_db.py, test_swarm.py, test_cli.py, test_watcher.py  # MODIFY
```

---

### Task 1: Schema v4 - context + closed_at columns and accessors

**Files:**
- Modify: `iterm/db.py`
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces: schema v4 - `sessions` gains `workdir TEXT NOT NULL DEFAULT ''`, `spawn_prompt TEXT NOT NULL DEFAULT ''`, `closed_at REAL NOT NULL DEFAULT 0`.
- Produces: `db.set_session_context(conn, name, workdir, spawn_prompt) -> bool`
- Produces: `db.mark_closed(conn, name, ts) -> bool`, `db.clear_closed(conn, name) -> None`, `db.closed_sessions(conn, project=None) -> list[Row]` (closed_at != 0, ordered by name)
- Consumes: existing `register` (must also clear closed_at on re-register).

- [ ] **Step 1: Add failing tests to `iterm/test_db.py`**

In the schema-versioning block, change the fresh-version assertion and add the v3->v4 column check:

```python
    ok &= check("fresh connect stamps user_version = 4",
                conn.execute("PRAGMA user_version").fetchone()[0] == 4)
```

Extend the migration test's created columns check (it currently makes a v1 table). After the existing `mig` block, assert the new columns exist:

```python
    mrow = mig.execute("SELECT workdir, spawn_prompt, closed_at FROM sessions "
                       "WHERE name='migrated'").fetchone()
    ok &= check("v1 db migrates to v4 with context + closed_at columns",
                mig.execute("PRAGMA user_version").fetchone()[0] == 4
                and mrow["workdir"] == "" and mrow["spawn_prompt"] == ""
                and mrow["closed_at"] == 0)
```

Add a dedicated context/closed block (own temp DB so counts elsewhere are unaffected):

```python
    # --- session context + closed_at (restore/clean foundation) -------------
    cpath = _tmpdb()
    cconn = db.connect(cpath)
    db.register(cconn, "w", "SID-W", "worker", "proj", now=10.0)
    ok &= check("context defaults empty",
                db.get_session(cconn, "w")["workdir"] == ""
                and db.get_session(cconn, "w")["spawn_prompt"] == ""
                and db.get_session(cconn, "w")["closed_at"] == 0)
    ok &= check("set_session_context stores both",
                db.set_session_context(cconn, "w", "/work/api", "build the API")
                and db.get_session(cconn, "w")["workdir"] == "/work/api"
                and db.get_session(cconn, "w")["spawn_prompt"] == "build the API")
    ok &= check("set_session_context unknown -> False",
                not db.set_session_context(cconn, "ghost", "/x", "y"))
    ok &= check("mark_closed stamps ts",
                db.mark_closed(cconn, "w", 500.0)
                and db.get_session(cconn, "w")["closed_at"] == 500.0)
    ok &= check("closed_sessions lists it",
                [r["name"] for r in db.closed_sessions(cconn)] == ["w"])
    # re-register revives (clears closed_at); keeps workdir/spawn_prompt.
    db.register(cconn, "w", "SID-W2", "worker", "proj", now=600.0)
    ok &= check("re-register clears closed_at",
                db.get_session(cconn, "w")["closed_at"] == 0
                and db.closed_sessions(cconn) == [])
    ok &= check("re-register keeps workdir",
                db.get_session(cconn, "w")["workdir"] == "/work/api")
    db.mark_closed(cconn, "w", 700.0)
    db.clear_closed(cconn, "w")
    ok &= check("clear_closed resets to 0",
                db.get_session(cconn, "w")["closed_at"] == 0)
    cconn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_db.py`
Expected: FAIL (user_version 3 != 4; `set_session_context` missing).

- [ ] **Step 3: Implement in `iterm/db.py`**

Add the columns to `_SCHEMA`'s `sessions` table (after `mode`):

```python
  mode TEXT NOT NULL DEFAULT '',
  workdir TEXT NOT NULL DEFAULT '',
  spawn_prompt TEXT NOT NULL DEFAULT '',
  closed_at REAL NOT NULL DEFAULT 0
```

Bump the ladder:

```python
_CURRENT_VERSION = 4
_MIGRATIONS = {
    1: ("ALTER TABLE sessions ADD COLUMN arm_request TEXT NOT NULL DEFAULT ''",),
    2: ("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT ''",),
    3: ("ALTER TABLE sessions ADD COLUMN workdir TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN spawn_prompt TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN closed_at REAL NOT NULL DEFAULT 0"),
}
```

Make `register` clear `closed_at` on the conflict path. In `register`'s
`ON CONFLICT(name) DO UPDATE SET` clause, add `closed_at=0` (a re-register
revives a closed session):

```python
           ON CONFLICT(name) DO UPDATE SET
             iterm_session_id=excluded.iterm_session_id,
             role=excluded.role,
             project=CASE WHEN excluded.project = ''
                          THEN sessions.project
                          ELSE excluded.project END,
             last_seen=excluded.last_seen,
             closed_at=0""",
```

Add accessors (near `set_session_mode`):

```python
def set_session_context(conn, name: str, workdir: str,
                        spawn_prompt: str) -> bool:
    """Persist where a session was spawned and its original mission, so a dead
    session can be restored in the right place with context."""
    cur = conn.execute(
        "UPDATE sessions SET workdir=?, spawn_prompt=? WHERE name=?",
        (workdir, spawn_prompt, name))
    conn.commit()
    return cur.rowcount > 0


def mark_closed(conn, name: str, ts: float) -> bool:
    cur = conn.execute("UPDATE sessions SET closed_at=? WHERE name=?",
                       (ts, name))
    conn.commit()
    return cur.rowcount > 0


def clear_closed(conn, name: str) -> None:
    conn.execute("UPDATE sessions SET closed_at=0 WHERE name=?", (name,))
    conn.commit()


def closed_sessions(conn, project=None):
    if project is None:
        return conn.execute("SELECT * FROM sessions WHERE closed_at != 0 "
                            "ORDER BY name").fetchall()
    return conn.execute("SELECT * FROM sessions WHERE closed_at != 0 "
                        "AND project=? ORDER BY name", (project,)).fetchall()
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_db.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`. (Update any other test that hardcoded user_version 3 - grep `user_version` in test files.)

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "db: schema v4 - session workdir/spawn_prompt/closed_at + accessors"
```

---

### Task 2: db - reset/delete/prune for clean

**Files:**
- Modify: `iterm/db.py`
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces: `db.reset_owner_tasks(conn, owner, now=None) -> int` - sets every non-`done` task with this owner to `state='todo', owner=NULL`; returns count.
- Produces: `db.delete_session(conn, name) -> None`
- Produces: `db.delete_undelivered_to(conn, name) -> int` - deletes queued (delivered_at IS NULL) messages addressed to `name`; returns count.
- Produces: `db.prune_messages(conn, older_than_days, now=None) -> int` - deletes DELIVERED messages with `created_at < now - days*86400`; returns count.

- [ ] **Step 1: Add failing tests to `iterm/test_db.py`** (own temp DB)

```python
    # --- clean helpers ------------------------------------------------------
    kpath = _tmpdb()
    k = db.connect(kpath)
    db.register(k, "dead", "SID-D", "worker", "p", now=1.0)
    e = db.add_task(k, "epic", project="p", owner="dead", now=2.0)
    s = db.add_task(k, "sub", project="p", owner="dead", parent_id=e, now=3.0)
    db.set_task_state(k, s, "doing", now=4.0)
    done = db.add_task(k, "finished", project="p", owner="dead", now=5.0)
    db.set_task_state(k, done, "done", now=6.0)
    n = db.reset_owner_tasks(k, "dead")
    ok &= check("reset_owner_tasks resets non-done owned tasks", n == 2)
    ok &= check("reset -> todo + unowned",
                db.get_task(k, e)["state"] == "todo"
                and db.get_task(k, e)["owner"] is None
                and db.get_task(k, s)["state"] == "todo")
    ok &= check("reset leaves done tasks alone",
                db.get_task(k, done)["state"] == "done"
                and db.get_task(k, done)["owner"] == "dead")

    db.queue_message(k, "coord", "dead", "you there?", "p", now=7.0)
    db.queue_message(k, "coord", "dead", "delivered one", "p", now=8.0)
    # mark one delivered so only the queued one is dropped
    mid = db.undelivered(k, "dead")[1]["id"]
    db.mark_delivered(k, mid, now=9.0)
    dn = db.delete_undelivered_to(k, "dead")
    ok &= check("delete_undelivered_to drops only queued", dn == 1)

    db.delete_session(k, "dead")
    ok &= check("delete_session removes the row",
                db.get_session(k, "dead") is None)

    # prune_messages: delivered + old only
    db.register(k, "x", "SID-X", "worker", "p", now=10.0)
    old = db.queue_message(k, "x", "coord", "old", "p", now=100.0)
    db.mark_delivered(k, old, now=101.0)
    new = db.queue_message(k, "x", "coord", "new", "p", now=1_000_000.0)
    db.mark_delivered(k, new, now=1_000_001.0)
    qd = db.queue_message(k, "x", "coord", "still queued", "p", now=100.0)
    pn = db.prune_messages(k, older_than_days=7, now=1_000_100.0)
    ok &= check("prune_messages drops old delivered only", pn == 1)
    ok &= check("prune keeps queued + recent",
                any(m["id"] == qd for m in db.undelivered(k)))
    k.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_db.py` -> FAIL (`reset_owner_tasks` missing).

- [ ] **Step 3: Implement in `iterm/db.py`**

```python
def reset_owner_tasks(conn, owner: str, now=None) -> int:
    """Send every non-done task owned by `owner` back to unowned todo (used by
    clean when giving up on a dead session)."""
    cur = conn.execute(
        "UPDATE tasks SET state='todo', owner=NULL, updated_at=? "
        "WHERE owner=? AND state!='done'", (_now(now), owner))
    conn.commit()
    return cur.rowcount


def delete_session(conn, name: str) -> None:
    conn.execute("DELETE FROM sessions WHERE name=?", (name,))
    conn.commit()


def delete_undelivered_to(conn, name: str) -> int:
    cur = conn.execute(
        "DELETE FROM messages WHERE to_name=? AND delivered_at IS NULL",
        (name,))
    conn.commit()
    return cur.rowcount


def prune_messages(conn, older_than_days: float, now=None) -> int:
    """Drop delivered messages older than the retention window. Queued
    (undelivered) messages are always kept."""
    cutoff = _now(now) - older_than_days * 86400
    cur = conn.execute(
        "DELETE FROM messages WHERE delivered_at IS NOT NULL AND created_at < ?",
        (cutoff,))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_db.py` then `./test/run.sh` -> all pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "db: clean helpers - reset_owner_tasks, delete_session, prune_messages"
```

---

### Task 3: swarm.py - pure restore/clean plan builders

**Files:**
- Modify: `iterm/swarm.py`
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Consumes: rows as dicts (sessions with name/role/project/workdir/spawn_prompt/closed_at; tasks with id/state/owner).
- Produces: `swarm.restore_candidates(sessions, tasks, names=None) -> list[dict]` - each candidate: `{name, role, project, workdir, spawn_prompt, task_ids: [int], live: bool}`. With `names=None`: sessions where `closed_at != 0` that own >=1 non-done task. With `names`: those named sessions that own >=1 non-done task, regardless of closed_at (`live` = closed_at == 0). Sorted by name.
- Produces: `swarm.clean_candidates(sessions, tasks) -> list[dict]` - each: `{name, task_ids: [int]}` for every closed session (owning tasks or not); task_ids = its non-done tasks.
- Produces: `swarm.restore_plan_text(cands, spawn_arm) -> str`, `swarm.clean_plan_text(cands) -> str` (plain text; the SKIP/zombie/arm-warning lines per spec section 4-5).
- Produces: `swarm.resume_prompt(name, project, role, spawn_prompt) -> str`.

- [ ] **Step 1: Add failing tests to `iterm/test_swarm.py`** (extend imports)

```python
from swarm import (  # add to existing import
    restore_candidates, clean_candidates, restore_plan_text, clean_plan_text,
    resume_prompt,
)
```

```python
    # --- restore/clean planning ---------------------------------------------
    S = [
        {"name": "bff", "role": "worker", "project": "shop", "workdir": "/w/bff",
         "spawn_prompt": "bff work", "closed_at": 500.0},
        {"name": "api", "role": "worker", "project": "shop", "workdir": "",
         "spawn_prompt": "", "closed_at": 900.0},
        {"name": "live", "role": "worker", "project": "shop", "workdir": "/w/l",
         "spawn_prompt": "", "closed_at": 0.0},
    ]
    T = [
        {"id": 1, "state": "doing", "owner": "bff"},
        {"id": 2, "state": "done", "owner": "bff"},
        {"id": 3, "state": "todo", "owner": "api"},
        {"id": 4, "state": "doing", "owner": "live"},
    ]
    auto = restore_candidates(S, T)
    ok &= check("auto restore = closed sessions owning non-done work",
                [c["name"] for c in auto] == ["api", "bff"])
    ok &= check("candidate carries task ids (non-done only)",
                next(c for c in auto if c["name"] == "bff")["task_ids"] == [1])
    named = restore_candidates(S, T, names=["live"])
    ok &= check("named restore includes a live session",
                len(named) == 1 and named[0]["name"] == "live"
                and named[0]["live"] is True)
    ok &= check("named restore of a session owning no non-done work -> empty",
                restore_candidates(S, T, names=["nobody"]) == [])

    txt = restore_plan_text(auto, spawn_arm="wild")
    ok &= check("plan shows workdir + tasks", "/w/bff" in txt and "#1" in txt)
    ok &= check("plan flags no-workdir candidate", "SKIP api" in txt)
    ok &= check("plan no arm warning when armed", "will not act" not in txt)
    ok &= check("plan warns when spawn_arm off",
                "will not act" in restore_plan_text(auto, spawn_arm="off"))
    ok &= check("named-live plan notes zombie tab",
                "zombie" in restore_plan_text(named, spawn_arm="wild"))

    cc = clean_candidates(S, T)
    ok &= check("clean candidates = all closed sessions",
                [c["name"] for c in cc] == ["api", "bff"])
    ok &= check("clean plan resets + removes",
                "reset" in clean_plan_text(cc) and "remove" in clean_plan_text(cc))

    rp = resume_prompt("bff", "shop", "worker", "bff work")
    ok &= check("resume prompt invokes skill + RESUMING + mission",
                "relay-worker" in rp and "RESUMING" in rp and "bff work" in rp
                and "relay task list --mine" in rp)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_swarm.py` -> ImportError.

- [ ] **Step 3: Implement in `iterm/swarm.py`**

```python
# --- restore / clean planning (pure; rows in, plans out) ----------------------

def _nondone_ids(tasks, owner):
    return [t["id"] for t in tasks
            if t["owner"] == owner and t["state"] != "done"]


def restore_candidates(sessions, tasks, names=None):
    """Sessions to revive. Auto (names=None): closed sessions owning non-done
    work. Manual (names given): those named sessions owning non-done work,
    regardless of closed state. Sorted by name."""
    out = []
    for s in sorted(sessions, key=lambda r: r["name"]):
        if names is None:
            if not s["closed_at"]:
                continue
        elif s["name"] not in names:
            continue
        ids = _nondone_ids(tasks, s["name"])
        if not ids:
            continue
        out.append({"name": s["name"], "role": s["role"],
                    "project": s["project"], "workdir": s["workdir"],
                    "spawn_prompt": s["spawn_prompt"], "task_ids": ids,
                    "live": not s["closed_at"]})
    return out


def clean_candidates(sessions, tasks):
    """Every closed session (whether or not it owns work), with its non-done
    task ids."""
    return [{"name": s["name"], "task_ids": _nondone_ids(tasks, s["name"])}
            for s in sorted(sessions, key=lambda r: r["name"])
            if s["closed_at"]]


def restore_plan_text(cands, spawn_arm: str) -> str:
    lines = ["RESTORE PLAN"]
    for c in cands:
        ids = " ".join(f"#{i}" for i in c["task_ids"])
        if not c["workdir"]:
            lines.append(f"  SKIP {c['name']} - no known workdir "
                         f"(use relay clean, or re-run relay in the dir)")
            continue
        zombie = "  [tab still open - old tab left as a zombie]" if c["live"] else ""
        lines.append(f"  restore {c['name']} ({c['role']}) in {c['workdir']} "
                     f"- {len(c['task_ids'])} task(s): {ids}{zombie}")
    if spawn_arm == "off":
        lines.append("  WARNING: spawn_arm is off - restored workers will not "
                     "act unattended (arm them, or set [swarm] spawn_arm)")
    return "\n".join(lines)


def clean_plan_text(cands) -> str:
    lines = ["CLEAN PLAN"]
    for c in cands:
        n = len(c["task_ids"])
        reset = f"reset {n} task(s) to todo, " if n else ""
        lines.append(f"  {reset}remove session {c['name']}")
    if len(lines) == 1:
        lines.append("  (nothing to clean)")
    return "\n".join(lines)


def resume_prompt(name: str, project: str, role: str, spawn_prompt: str) -> str:
    skill = "relay-worker" if role == "worker" else "relay-coordinator"
    p = (f"Invoke the {skill} skill. You are '{name}'"
         + (f" on project '{project}'" if project else "")
         + ", RESUMING work a previous session left unfinished. Run "
         f"`relay task list --mine` and `relay inbox`, then continue the "
         f"in-progress task(s) from where they were left.")
    if spawn_prompt:
        p += f" Original mission: {spawn_prompt}"
    return p
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_swarm.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "swarm: pure restore/clean plan builders + resume prompt"
```

---

### Task 4: spawn records workdir + mission; register --dir

**Files:**
- Modify: `iterm/spawn.py`, `iterm/cli.py`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.set_session_context` (Task 1).
- Produces: `spawn_worker` writes context after registering; `cmd_register` accepts `--dir` and records it.

- [ ] **Step 1: Add failing test to `iterm/test_cli.py`** (before `conn.close()`)

```python
    # register --dir records workdir
    code, _, _ = run_cli("register", "--name", "ctxw", "--role", "worker",
                         "--project", "p", "--dir", "/work/ctx",
                         iterm_id="w0t9p0:CTX-ID")
    ok &= check("register --dir stores workdir",
                code == 0 and db.get_session(conn, "ctxw")["workdir"] == "/work/ctx")
```

- [ ] **Step 2: Run -> FAIL** (`register` has no `--dir`).

- [ ] **Step 3: Implement**

`iterm/spawn.py` - after the `db.register(...)` / arm block, record context (use the resolved workdir and the mission `prompt`):

```python
    if arm != "off":
        db.set_arm_request(conn, name, arm)
    db.set_session_context(conn, name, workdir, prompt)
```

`iterm/cli.py` `cmd_register` - after computing `name`, pass workdir. Change the register call and add the parser arg. In `cmd_register`:

```python
    conn = db.connect()
    db.register(conn, name, sid, args.role, args.project or "")
    if args.dir:
        db.set_session_context(conn, name, os.path.abspath(args.dir),
                               db.get_session(conn, name)["spawn_prompt"])
```

In `build_parser()` under the `register` subparser:

```python
    r.add_argument("--dir", default=None,
                   help="record this session's working directory (for restore)")
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_cli.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/spawn.py iterm/cli.py iterm/test_cli.py
git commit -m "swarm: spawn records workdir+mission; register --dir"
```

---

### Task 5: Watcher - closed_at marking (debounced, sync-gated) + orphan_count

**Files:**
- Modify: `iterm/watcher.py`
- Test: `iterm/test_watcher.py`

**Interfaces:**
- Consumes: `db.list_sessions`, `db.mark_closed`, `db.clear_closed`, `db.list_tasks`.
- Produces: `Watcher._mark_closed_sessions()` called each tick after a SUCCESSFUL roster sync; `Watcher.orphan_count` (int) = closed sessions owning >=1 non-done task; `SessionInfo` unaffected. New init state: `self._miss = {}` (name -> consecutive miss count), `self.orphan_count = 0`, `self.close_misses = 2`.

- [ ] **Step 1: Add failing tests to `iterm/test_watcher.py`** (new `closed_tests()` in the `__main__` chain)

```python
def closed_tests():
    """closed_at marking is debounced and only runs after a good roster sync."""
    from watcher import Watcher
    import config as C
    ok = True
    def chk(n, c):
        nonlocal ok
        print(("PASS" if c else "FAIL"), n); ok = ok and c

    marked, cleared = [], []
    W.swarmdb.mark_closed = lambda conn, name, ts: marked.append(name)
    W.swarmdb.clear_closed = lambda conn, name: cleared.append(name)
    W.swarmdb.list_tasks = lambda conn, project=None, owner=None: []

    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()
    # DB says 'w1' registered (not closed); live tabs = {} (its tab is gone).
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 0}]
    w.sessions = {}   # no live tabs

    w._mark_closed_sessions()
    chk("miss 1: not yet marked", marked == [])
    w._mark_closed_sessions()
    chk("miss 2: marked closed", marked == ["w1"])
    # once the DB row reflects closed_at != 0, the `not closed` guard stops a
    # re-mark. Simulate that by having list_sessions now report it closed.
    marked.clear()
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 123.0}]
    w._mark_closed_sessions()
    chk("already-closed row is not re-marked", marked == [])

    # tab reappears -> miss counter resets, closed cleared
    w.sessions = {"S1": object()}
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 999.0}]
    w._mark_closed_sessions()
    chk("reappeared -> clear_closed", cleared == ["w1"])

    # orphan_count: 1 closed session owning a non-done task
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "d", "iterm_session_id": "SD", "role": "worker",
         "project": "p", "closed_at": 500.0}]
    W.swarmdb.list_tasks = lambda conn, project=None, owner=None: [
        {"id": 1, "state": "doing", "owner": "d"}]
    w.sessions = {}
    w._recount_orphans()
    chk("orphan_count counts closed owners of non-done work", w.orphan_count == 1)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

Add to `__main__`: `r5 = closed_tests()` and include in the exit expr.

- [ ] **Step 2: Run -> FAIL** (`_mark_closed_sessions` missing).

- [ ] **Step 3: Implement in `iterm/watcher.py`**

Add init state (with the other swarm state):

```python
        self._miss = {}            # session name -> consecutive missed ticks
        self.close_misses = 2      # misses before marking closed (debounce)
        self.orphan_count = 0      # closed sessions owning non-done work
        self._roster_ok = False    # did THIS tick's sync succeed?
```

In `start()`, set `_roster_ok` around the sync and call the marker only when true. Replace the sync block:

```python
                try:
                    app = await iterm2.async_get_app(self.connection)
                    await self._sync_sessions(app)
                    self._roster_ok = True
                except Exception as e:
                    self._roster_ok = False
                    self._note(f"roster sync error: {e}")
                self._swarm_refresh_registry()
                if self._roster_ok:
                    self._mark_closed_sessions()
                self._check_gone()
```

Add the methods (after `_swarm_refresh_registry`):

```python
    def _mark_closed_sessions(self) -> None:
        """After a good roster sync: a registered session whose tab is missing
        for close_misses consecutive ticks is stamped closed; a reappeared tab
        resets the counter and clears closed_at. The debounce + sync gate stop
        a transient empty roster from false-marking a live swarm."""
        try:
            conn = self._swarm_conn()
            rows = swarmdb.list_sessions(conn)
        except Exception as e:
            self._note(f"swarm db error: {e}")
            return
        live = set(self.sessions.keys())
        for r in rows:
            name, sid, closed = r["name"], r["iterm_session_id"], r["closed_at"]
            if sid in live:
                self._miss.pop(name, None)
                if closed:
                    try:
                        swarmdb.clear_closed(conn, name)
                    except Exception:
                        pass
                continue
            self._miss[name] = self._miss.get(name, 0) + 1
            if self._miss[name] >= self.close_misses and not closed:
                try:
                    swarmdb.mark_closed(conn, name, time.time())
                    self._note(f"CLOSED {name} (tab gone)")
                except Exception:
                    pass
        self._recount_orphans()

    def _recount_orphans(self) -> None:
        try:
            conn = self._swarm_conn()
            closed = {r["name"] for r in swarmdb.list_sessions(conn)
                      if r["closed_at"]}
            owners = {t["owner"] for t in swarmdb.list_tasks(conn)
                      if t["state"] != "done" and t["owner"]}
            self.orphan_count = len(closed & owners)
        except Exception:
            pass
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_watcher.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Live smoke (deferred to human)** - note in report only: launch relay, close a worker's tab, confirm `CLOSED <name>` appears in the log within ~2 ticks and does NOT fire for live tabs.

- [ ] **Step 6: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "watcher: mark sessions closed when their tab vanishes (debounced, sync-gated)"
```

---

### Task 6: CLI - relay clean

**Files:**
- Modify: `iterm/cli.py`, `bin/relay`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `swarm.clean_candidates/clean_plan_text`, `db.closed_sessions/list_tasks/reset_owner_tasks/delete_session/delete_undelivered_to`.
- Produces: verb `clean [--project P] [--yes] [--dry-run]`.

- [ ] **Step 1: Add failing tests to `iterm/test_cli.py`**

```python
    # --- clean: reset + remove closed sessions, plan/dry-run/confirm ---------
    import db as _db
    cc = db.connect()
    # a closed session owning a doing task
    _db.register(cc, "deadw", "DW", "worker", "webshop", now=1.0)
    ct = _db.add_task(cc, "half done", project="webshop", owner="deadw", now=2.0)
    _db.set_task_state(cc, ct, "doing", now=3.0)
    _db.mark_closed(cc, "deadw", 400.0)

    code, out, _ = run_cli("clean", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("clean --dry-run shows plan, changes nothing",
                code == 0 and "deadw" in out
                and db.get_session(cc, "deadw") is not None
                and db.get_task(cc, ct)["state"] == "doing")
    code, out, _ = run_cli("clean", "--project", "webshop", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("clean --yes resets task to unowned todo",
                code == 0 and db.get_task(cc, ct)["state"] == "todo"
                and db.get_task(cc, ct)["owner"] is None)
    ok &= check("clean --yes removes the closed session row",
                db.get_session(cc, "deadw") is None)
    cc.close()
```

- [ ] **Step 2: Run -> FAIL** (`clean` unknown verb -> exit 2).

- [ ] **Step 3: Implement `cmd_clean` in `iterm/cli.py`**

```python
def cmd_clean(args) -> int:
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    cands = swarm.clean_candidates(sessions, tasks)
    print(swarm.clean_plan_text(cands))
    if not cands or args.dry_run:
        return 0
    if not args.yes and not _confirm(f"clean {len(cands)} session(s)?"):
        print("aborted.")
        return 0
    for c in cands:
        db.reset_owner_tasks(conn, c["name"])
        db.delete_undelivered_to(conn, c["name"])
        db.delete_session(conn, c["name"])
    print(f"cleaned {len(cands)} session(s).")
    return 0
```

Add a `_confirm` helper (near `_err`):

```python
def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
```

Parser (before `return p`):

```python
    cl = sub.add_parser("clean", help="reset abandoned tasks + remove dead "
                                      "sessions")
    cl.add_argument("--project", default=None)
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--dry-run", dest="dry_run", action="store_true")
    cl.set_defaults(fn=cmd_clean)
```

`bin/relay`: add `clean` to the dispatch `case` verb list and the `-h` header;
extend the `sed -n '2,NNp'` range if you add a help line.

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_cli.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py bin/relay iterm/test_cli.py
git commit -m "swarm: relay clean - reset abandoned tasks, remove dead sessions"
```

---

### Task 7: CLI - relay restore

**Files:**
- Modify: `iterm/cli.py`, `bin/relay`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `swarm.restore_candidates/restore_plan_text/resume_prompt`, `db.list_sessions/list_tasks`, `config.load`, `spawn.spawn_worker`.
- Produces: verb `restore [names...] [--project P] [--yes] [--dry-run]`. `--dry-run` and the plan are pure (no iTerm2); only the actual spawn imports `spawn`.

- [ ] **Step 1: Add failing tests to `iterm/test_cli.py`** (plan/dry-run only - the real spawn is smoke-tested live)

```python
    # --- restore: plan + dry-run (spawn side is live-only) ------------------
    rc = db.connect()
    import db as _db2
    _db2.register(rc, "rw", "RW", "worker", "webshop", now=1.0)
    _db2.set_session_context(rc, "rw", "/work/rw", "do the thing")
    rt = _db2.add_task(rc, "unfinished", project="webshop", owner="rw", now=2.0)
    _db2.set_task_state(rc, rt, "doing", now=3.0)
    _db2.mark_closed(rc, "rw", 500.0)

    code, out, _ = run_cli("restore", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("restore --dry-run plans, spawns nothing",
                code == 0 and "restore rw" in out and "/work/rw" in out
                and "#" + str(rt) in out
                and db.get_session(rc, "rw")["closed_at"] == 500.0)

    # no-workdir closed session is SKIPped
    _db2.register(rc, "nowd", "NW", "worker", "webshop", now=4.0)
    nt = _db2.add_task(rc, "x", project="webshop", owner="nowd", now=5.0)
    _db2.set_task_state(rc, nt, "doing", now=6.0)
    _db2.mark_closed(rc, "nowd", 500.0)
    code, out, _ = run_cli("restore", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("restore skips no-workdir session", "SKIP nowd" in out)
    rc.close()
```

- [ ] **Step 2: Run -> FAIL** (`restore` unknown verb).

- [ ] **Step 3: Implement `cmd_restore` in `iterm/cli.py`**

```python
def cmd_restore(args) -> int:
    import config as relay_config
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.list_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.restore_candidates(sessions, tasks, names=names)
    spawn_arm = relay_config.load()[0].spawn_arm
    print(swarm.restore_plan_text(cands, spawn_arm))
    # only candidates we can actually revive (have a workdir)
    doable = [c for c in cands if c["workdir"]]
    if not doable or args.dry_run:
        return 0
    if not args.yes and not _confirm(f"restore {len(doable)} session(s)?"):
        print("aborted.")
        return 0
    import asyncio
    import spawn as spawnmod
    for c in doable:
        prompt = swarm.resume_prompt(c["name"], c["project"], c["role"],
                                     c["spawn_prompt"])
        asyncio.run(spawnmod.spawn_worker(
            c["name"], c["project"], prompt, c["workdir"], c["role"],
            arm=spawn_arm))
        print(f"restored {c['name']} in {c['workdir']}")
    return 0
```

Parser (before `return p`):

```python
    rs = sub.add_parser("restore", help="respawn dead workers in their workdir "
                                        "to finish their tasks")
    rs.add_argument("names", nargs="*", help="specific sessions to restore "
                    "(default: all closed sessions owning work)")
    rs.add_argument("--project", default=None)
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--dry-run", dest="dry_run", action="store_true")
    rs.set_defaults(fn=cmd_restore)
```

`bin/relay`: add `restore` to the dispatch `case` and `-h` header (extend the
`sed` range as needed).

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_cli.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Live smoke (deferred to human)** - report only: with the running relay, a real closed worker owning a doing task; `relay restore --project X` shows the plan, confirm, a new tab opens in the right dir, re-registers, resumes.

- [ ] **Step 6: Commit**

```bash
git add iterm/cli.py bin/relay iterm/test_cli.py
git commit -m "swarm: relay restore - respawn dead workers in their workdir"
```

---

### Task 8: doctor orphans + auto message retention

**Files:**
- Modify: `iterm/cli.py` (doctor), `iterm/app.py` (launch prune)
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.closed_sessions`, `db.prune_messages`.
- Produces: `relay doctor` prints an `orphans:` line; the TUI prunes old delivered messages at launch.

- [ ] **Step 1: Add failing test to `iterm/test_cli.py`**

```python
    # doctor reports orphans (closed session owning non-done work)
    dc = db.connect()
    import db as _db3
    _db3.register(dc, "orph", "OR", "worker", "webshop", now=1.0)
    ot = _db3.add_task(dc, "stuck", project="webshop", owner="orph", now=2.0)
    _db3.set_task_state(dc, ot, "doing", now=3.0)
    _db3.mark_closed(dc, "orph", 400.0)
    code, out, _ = run_cli("doctor")
    ok &= check("doctor reports orphaned work",
                code == 0 and "orphan" in out.lower() and "orph" in out)
    dc.close()
```

- [ ] **Step 2: Run -> FAIL** (no orphans line).

- [ ] **Step 3: Implement**

In `cmd_doctor`, after the tasks block, add:

```python
    closed = db.closed_sessions(conn)
    owners = {t["owner"] for t in tasks if t["state"] != "done" and t["owner"]}
    orphans = [s for s in closed if s["name"] in owners]
    if orphans:
        print(f"  orphans: {len(orphans)} closed session(s) still own work "
              f"- 'relay restore' to revive, 'relay clean' to reset")
        for s in orphans:
            print(f"    {s['name']} (workdir: {s['workdir'] or 'unknown'})")
```

(Note: `tasks` in `cmd_doctor` is `db.list_tasks(conn)` - confirm it is fetched
before this block; if the current code only counts states, add
`tasks = db.list_tasks(conn)` above.)

In `iterm/app.py` `on_mount`, next to the existing `audit.prune_old()` call,
add message retention:

```python
        try:
            import db as _swarmdb
            _swarmdb.prune_messages(
                _swarmdb.connect(),
                float(os.environ.get("RELAY_MSG_RETENTION_DAYS", "7")))
        except Exception:
            pass
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_cli.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/app.py iterm/test_cli.py
git commit -m "swarm: doctor lists orphans; TUI prunes old delivered messages"
```

---

### Task 9: TUI orphan hint + R restore

**Files:**
- Modify: `iterm/app.py`

**Interfaces:**
- Consumes: `watcher.orphan_count`.
- Produces: subtitle orphan hint; `R` binding that shells out to `relay restore --yes` after a two-press confirm.

- [ ] **Step 1: Implement (UI-only; validated by test_app render + live)**

In `BINDINGS`, add:

```python
        Binding("R", "restore", "Restore orphaned", show=True),
```

In `__init__`: `self._restore_armed = False`.

In `_refresh`, extend the subtitle hint logic - after computing `n_ctrl`, add an
orphan hint (highest priority):

```python
        orphans = getattr(self.watcher, "orphan_count", 0)
        if orphans:
            hint = (f"  [#ff5555]· {orphans} task-owner(s) dead - press R to "
                    f"restore, or run 'relay clean'[/]")
        elif n_ctrl == 0:
            ...
```

(Fold into the existing if/elif chain so orphans win.)

Add the action:

```python
    def action_restore(self) -> None:
        if not getattr(self.watcher, "orphan_count", 0):
            return
        if not self._restore_armed:
            self._restore_armed = True
            self.set_timer(3.0, lambda: setattr(self, "_restore_armed", False))
            self.query_one(Log).write_line(
                "restore: press R again within 3s to respawn dead workers")
            return
        self._restore_armed = False
        here = os.path.dirname(os.path.abspath(__file__))
        relay_bin = os.path.join(here, "..", "bin", "relay")
        try:
            subprocess.Popen([relay_bin, "restore", "--yes"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.query_one(Log).write_line("restore: launching dead workers...")
        except Exception as e:
            self.query_one(Log).write_line(f"restore failed: {e}")
```

- [ ] **Step 2: Run the suite** (test_app drives the real app; confirms render + bindings load)

Run: `./test/run.sh` -> `ALL SUITES PASSED`.

- [ ] **Step 3: Live check (deferred to human)** - report only: close a worker tab, see the red orphan hint, press R twice, a tab respawns.

- [ ] **Step 4: Commit**

```bash
git add iterm/app.py
git commit -m "TUI: orphan hint + R to restore dead workers"
```

---

### Task 10: README + final sweep

**Files:**
- Modify: `README.md`, `docs/specs/2026-07-15-swarm-restore-design.md`

- [ ] **Step 1: README** - in the Swarm section, add a "Recovering abandoned work" subsection: what a closed/dead session is, `relay restore [names]` (plan, confirm, respawn in workdir; stalled-tab zombie caveat; arm warning), `relay clean` (reset to todo + remove ghosts; opposite of restore, run restore first if unsure), the `R` key, and `relay doctor`'s orphans line. Note `RELAY_MSG_RETENTION_DAYS` in the env table. No em-dash.

- [ ] **Step 2: Spec status** - change `**Status:** Approved for planning` to `**Status:** Implemented (see docs/plans/2026-07-15-swarm-restore.md)`.

- [ ] **Step 3: Final sweep**

```bash
./test/run.sh                       # ALL SUITES PASSED
grep -rn $'—' iterm/ README.md docs/specs/2026-07-15-swarm-restore-design.md || echo "no em-dashes"
python3 - <<'EOF'
import sys; sys.path.insert(0, "iterm")
import swarm
S=[{"name":"d","role":"worker","project":"p","workdir":"/w","spawn_prompt":"m","closed_at":9.0}]
T=[{"id":1,"state":"doing","owner":"d"}]
print(swarm.restore_plan_text(swarm.restore_candidates(S,T), "wild"))
print(swarm.clean_plan_text(swarm.clean_candidates(S,T)))
EOF
```

Expected: suite green, "no em-dashes", a restore plan naming `/w #1` and a clean plan.

Live end-to-end (HUMAN, deferred - list in report, do not run): spawn a worker in a real dir, let it take a task, close its tab; `relay doctor` shows the orphan; `relay restore --dry-run` then `relay restore` respawns it in the right dir and it resumes; separately, `relay clean` on another dead worker resets its task to todo and removes the row.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/specs/2026-07-15-swarm-restore-design.md
git commit -m "docs: document swarm restore/clean, mark spec implemented"
```
