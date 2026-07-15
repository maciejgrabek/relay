# Swarm Wipe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `relay wipe` - the delete-counterpart to `clean`: delete closed sessions' tasks + ghost rows (orphaned scope), or nuke a whole project (`--all`), with plan/confirm/dry-run and a TUI `W` key.

**Architecture:** Pure planners in `swarm.py` (wipe_candidates / wipe_blocker_warnings / wipe_plan_text), two DB deleters in `db.py` (delete_tasks_for_owner, wipe_project), a `cmd_wipe` CLI verb mirroring `cmd_clean`, and a `W` TUI binding mirroring `R`. Spec: `docs/specs/2026-07-16-swarm-wipe-design.md`.

**Tech Stack:** Python 3 stdlib (`sqlite3`, `argparse`), existing `textual`. No new deps.

## Global Constraints

- NO em-dash (U+2014) anywhere. Plain `-` only.
- No pytest: `iterm/test_*.py` with `run()`/`__main__` runners (exit 0/1), auto-globbed by `test/run.sh`.
- `db.py` and `swarm.py` import neither `iterm2` nor (swarm.py) `sqlite3`.
- `wipe` acts on **closed** sessions only in orphaned mode (same candidate set as `clean`); `--all` requires `--project` (error otherwise) and deletes regardless of liveness. `wipe` never touches a live session in orphaned mode.
- orphaned wipe deletes ALL tasks owned by a closed session (any state, including `done`); `clean` reset only non-done - keep this distinction.
- Both modes print a plan; `--dry-run` stops; else confirm from stdin unless `--yes`. The confirm line says DELETE / permanently.
- Deletion keeps delivered message history (only `delete_undelivered_to` drops queued messages), matching `clean`.
- Commit after every task; short imperative subjects; no Co-Authored-By trailer.

## Reference: codebase facts

- `iterm/db.py`: `closed_sessions(conn, project=None)`, `list_tasks(conn, project=None, owner=None)`, `list_sessions(conn, project=None)`, `delete_session(conn, name)` (deletes ONLY the sessions row), `delete_undelivered_to(conn, name) -> int` (drops queued messages to name), `reset_owner_tasks(conn, owner, now=None) -> int`. `_now(now)` helper. Tables: `tasks(id, project, parent_id, title, state, owner, spec_path, blocked_by, created_by, updated_at)`, `sessions(name, ...)`, `messages(id, project, from_name, to_name, body, created_at, delivered_at)`.
- `iterm/swarm.py`: `clean_candidates(sessions, tasks)` returns `[{name, task_ids}]` for every closed session (task_ids = non-done owned). `clean_plan_text(cands)`. `parse_blockers(s) -> list[int]`. `_nondone_ids(tasks, owner)`.
- `iterm/cli.py`: `cmd_clean` pattern (connect; build dict rows; plan; dry-run/empty early-return; `_confirm` unless `--yes`; act). `_confirm(question) -> bool` (input(); False on EOF/KeyboardInterrupt). Verbs are argparse subparsers in `build_parser()`; each `cmd_*(args) -> int`.
- `iterm/app.py`: `Binding("R", "restore", ...)` at BINDINGS; `self._restore_armed = False` in `__init__`; `action_restore` two-press pattern (arm + `set_timer(3.0, ...)`, second press shells `subprocess.Popen([relay_bin, "restore", "--yes"], ...)`); subtitle hint at `_refresh` line ~356 (`if orphans: hint = "... press R to restore ..."`); `getattr(self.watcher, "orphan_count", 0)` guards.
- `bin/relay`: dispatch `case` verb list `register|send|status|task|inbox|msgs|spawn|doctor|version|update|clean|restore`; `-h` header line lists verbs; `sed -n '2,18p'` prints the header.
- Test idioms: `test_db.py` `_tmpdb()` + own temp DBs; `test_swarm.py` pure; `test_cli.py` sets RELAY_DB+ITERM_SESSION_ID before importing cli, `run_cli(*argv, iterm_id=...)`.

## File structure

```
iterm/db.py       # MODIFY: delete_tasks_for_owner, wipe_project
iterm/swarm.py    # MODIFY: wipe_candidates, wipe_blocker_warnings, wipe_plan_text
iterm/cli.py      # MODIFY: cmd_wipe + parser
bin/relay         # MODIFY: dispatch wipe + -h header
iterm/app.py      # MODIFY: W binding + action_wipe + subtitle hint
README.md         # MODIFY: wipe docs
iterm/test_db.py, test_swarm.py, test_cli.py  # MODIFY
```

---

### Task 1: db - delete_tasks_for_owner + wipe_project

**Files:**
- Modify: `iterm/db.py`
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces: `db.delete_tasks_for_owner(conn, owner) -> int` - `DELETE FROM tasks WHERE owner=?`; returns count.
- Produces: `db.wipe_project(conn, project) -> tuple[int,int,int]` - deletes all tasks, sessions, messages for `project`; returns `(n_tasks, n_sessions, n_messages)`.

- [ ] **Step 1: Add failing tests to `iterm/test_db.py`** (own temp DB block, before the final section that closes the main `conn`)

```python
    # --- wipe helpers -------------------------------------------------------
    wpath = _tmpdb()
    wdb = db.connect(wpath)
    db.register(wdb, "dead", "SID-WD", "worker", "proj", now=1.0)
    t1 = db.add_task(wdb, "a", project="proj", owner="dead", now=2.0)
    t2 = db.add_task(wdb, "b", project="proj", owner="dead", now=3.0)
    db.set_task_state(wdb, t2, "done", now=4.0)
    keep = db.add_task(wdb, "other", project="proj", owner="live", now=5.0)
    n = db.delete_tasks_for_owner(wdb, "dead")
    ok &= check("delete_tasks_for_owner deletes all owner's tasks (incl done)",
                n == 2 and db.get_task(wdb, t1) is None and db.get_task(wdb, t2) is None)
    ok &= check("delete_tasks_for_owner leaves other owners",
                db.get_task(wdb, keep) is not None)

    # wipe_project: everything for a project, other projects intact
    db.register(wdb, "s1", "S1", "worker", "P1", now=10.0)
    db.register(wdb, "s2", "S2", "worker", "P2", now=11.0)
    db.add_task(wdb, "p1t", project="P1", owner="s1", now=12.0)
    db.add_task(wdb, "p2t", project="P2", owner="s2", now=13.0)
    db.queue_message(wdb, "s1", "s2", "hi", "P1", now=14.0)
    db.queue_message(wdb, "s2", "s1", "yo", "P2", now=15.0)
    nt, ns, nm = db.wipe_project(wdb, "P1")
    ok &= check("wipe_project returns counts", nt == 1 and ns == 1 and nm == 1)
    ok &= check("wipe_project clears P1",
                db.list_tasks(wdb, project="P1") == []
                and db.get_session(wdb, "s1") is None)
    ok &= check("wipe_project leaves P2 intact",
                len(db.list_tasks(wdb, project="P2")) == 1
                and db.get_session(wdb, "s2") is not None
                and len(db.message_history(wdb, project="P2")) == 1)
    wdb.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_db.py`
Expected: FAIL (`delete_tasks_for_owner` missing).

- [ ] **Step 3: Implement in `iterm/db.py`** (near `delete_session`)

```python
def delete_tasks_for_owner(conn, owner: str) -> int:
    """Delete every task owned by `owner` (any state). Used by wipe to remove a
    dead session's work outright, vs reset_owner_tasks which only resets."""
    cur = conn.execute("DELETE FROM tasks WHERE owner=?", (owner,))
    conn.commit()
    return cur.rowcount


def wipe_project(conn, project: str) -> tuple:
    """Delete ALL tasks, sessions, and messages for one project - a blank slate.
    Returns (n_tasks, n_sessions, n_messages)."""
    nt = conn.execute("DELETE FROM tasks WHERE project=?", (project,)).rowcount
    ns = conn.execute("DELETE FROM sessions WHERE project=?",
                      (project,)).rowcount
    nm = conn.execute("DELETE FROM messages WHERE project=?",
                      (project,)).rowcount
    conn.commit()
    return (nt, ns, nm)
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_db.py` then `./test/run.sh` -> all pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "db: delete_tasks_for_owner + wipe_project for swarm wipe"
```

---

### Task 2: swarm - wipe planners

**Files:**
- Modify: `iterm/swarm.py`
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Consumes: rows as dicts (sessions with name/closed_at; tasks with id/owner/state/blocked_by).
- Produces: `swarm.wipe_candidates(sessions, tasks, names=None) -> list[dict]` - closed sessions (optionally filtered to `names`), each `{name, task_ids}` where task_ids = ALL tasks owned by name (any state). Sorted by name.
- Produces: `swarm.wipe_blocker_warnings(cands, tasks) -> list[str]` - for each task being wiped (in any candidate's task_ids) that appears in the `blocked_by` of a task NOT being wiped, a line `WARNING: #X is a blocker of #Y (not being wiped) - #Y may never unblock`.
- Produces: `swarm.wipe_plan_text(cands, project_all=None) -> str` - orphaned plan (per-candidate `delete N task(s), session <name>`); when `project_all=(nt,ns,nm)` render the `--all` totals form. Empty orphaned -> `(nothing to wipe)`.

- [ ] **Step 1: Add failing tests to `iterm/test_swarm.py`** (extend the import line)

```python
from swarm import (  # add to existing import
    wipe_candidates, wipe_blocker_warnings, wipe_plan_text,
)
```

```python
    # --- wipe planning ------------------------------------------------------
    WS = [
        {"name": "dead", "closed_at": 500.0},
        {"name": "live", "closed_at": 0.0},
    ]
    WT = [
        {"id": 1, "owner": "dead", "state": "doing", "blocked_by": ""},
        {"id": 2, "owner": "dead", "state": "done", "blocked_by": ""},
        {"id": 3, "owner": "live", "state": "todo", "blocked_by": "1"},
    ]
    wc = wipe_candidates(WS, WT)
    ok &= check("wipe candidates = closed sessions only",
                [c["name"] for c in wc] == ["dead"])
    ok &= check("wipe includes done tasks",
                sorted(wc[0]["task_ids"]) == [1, 2])
    ok &= check("wipe names filter to a closed session",
                [c["name"] for c in wipe_candidates(WS, WT, names=["dead"])] == ["dead"])
    ok &= check("wipe names filter excludes a live session",
                wipe_candidates(WS, WT, names=["live"]) == [])

    warns = wipe_blocker_warnings(wc, WT)
    ok &= check("blocker warning fires across the wipe boundary",
                any("#1 is a blocker of #3" in w for w in warns))
    # if the dependent is ALSO wiped, no warning
    WT2 = WT + [{"id": 4, "owner": "dead", "state": "todo", "blocked_by": "1"}]
    wc2 = wipe_candidates(WS, WT2)
    warns2 = wipe_blocker_warnings(wc2, WT2)
    ok &= check("no warning when dependent is also wiped",
                not any("#4" in w for w in warns2))

    txt = wipe_plan_text(wc)
    ok &= check("wipe plan lists session + task count",
                "dead" in txt and "delete" in txt.lower())
    ok &= check("empty wipe plan", "(nothing to wipe)" in wipe_plan_text([]))
    allt = wipe_plan_text([], project_all=(5, 2, 9))
    ok &= check("project --all plan shows totals",
                "5" in allt and "2" in allt and "9" in allt)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_swarm.py` -> ImportError.

- [ ] **Step 3: Implement in `iterm/swarm.py`** (after the clean planners)

```python
def _all_ids(tasks, owner):
    return [t["id"] for t in tasks if t["owner"] == owner]


def wipe_candidates(sessions, tasks, names=None):
    """Closed sessions to delete outright (optionally filtered to `names`),
    each with ALL its owned task ids (any state - wipe removes done too)."""
    out = []
    for s in sorted(sessions, key=lambda r: r["name"]):
        if not s["closed_at"]:
            continue
        if names is not None and s["name"] not in names:
            continue
        out.append({"name": s["name"], "task_ids": _all_ids(tasks, s["name"])})
    return out


def wipe_blocker_warnings(cands, tasks):
    """Warn when a task being wiped is a blocker of a task that is NOT being
    wiped - that dependent may never unblock once its blocker is gone."""
    wiped = set()
    for c in cands:
        wiped.update(c["task_ids"])
    out = []
    for t in tasks:
        if t["id"] in wiped:
            continue
        for b in parse_blockers(t["blocked_by"]):
            if b in wiped:
                out.append(f"WARNING: #{b} is a blocker of #{t['id']} "
                           f"(not being wiped) - #{t['id']} may never unblock")
    return out


def wipe_plan_text(cands, project_all=None) -> str:
    if project_all is not None:
        nt, ns, nm = project_all
        return ("WIPE PLAN (whole project)\n"
                f"  delete {nt} task(s), {ns} session(s), {nm} message(s)")
    lines = ["WIPE PLAN"]
    for c in cands:
        lines.append(f"  delete {len(c['task_ids'])} task(s), "
                     f"session {c['name']}")
    if len(lines) == 1:
        lines.append("  (nothing to wipe)")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_swarm.py` then `./test/run.sh` -> pass.

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "swarm: pure wipe planners + dangling-blocker warnings"
```

---

### Task 3: CLI - relay wipe

**Files:**
- Modify: `iterm/cli.py`, `bin/relay`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `swarm.wipe_candidates/wipe_blocker_warnings/wipe_plan_text`, `db.closed_sessions/list_tasks/list_sessions/delete_tasks_for_owner/delete_undelivered_to/delete_session/wipe_project/message_history`, `_confirm`.
- Produces: verb `wipe [names...] [--project P] [--all] [--yes] [--dry-run]`.

- [ ] **Step 1: Add failing tests to `iterm/test_cli.py`**

```python
    # --- wipe: orphaned + --all + guards ------------------------------------
    import db as _wdb
    wc = db.connect()
    _wdb.register(wc, "deadw", "DWX", "worker", "wp", now=1.0)
    wt = _wdb.add_task(wc, "gone", project="wp", owner="deadw", now=2.0)
    _wdb.set_task_state(wc, wt, "doing", now=3.0)
    _wdb.mark_closed(wc, "deadw", 400.0)

    code, out, _ = run_cli("wipe", "--project", "wp", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --dry-run plans, deletes nothing",
                code == 0 and "deadw" in out
                and db.get_task(wc, wt) is not None)
    code, out, _ = run_cli("wipe", "--project", "wp", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --yes deletes task + session",
                code == 0 and db.get_task(wc, wt) is None
                and db.get_session(wc, "deadw") is None)

    # --all requires --project
    code, _, err = run_cli("wipe", "--all", iterm_id="w0t0p0:CO-ID")
    ok &= check("--all without --project -> error", code == 1 and "project" in err)

    # --all nukes a project, leaves another intact
    _wdb.register(wc, "a1", "A1", "worker", "PA", now=1.0)
    _wdb.register(wc, "b1", "B1", "worker", "PB", now=1.0)
    _wdb.add_task(wc, "pa", project="PA", owner="a1", now=2.0)
    _wdb.add_task(wc, "pb", project="PB", owner="b1", now=2.0)
    code, out, _ = run_cli("wipe", "--project", "PA", "--all", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --all empties the project",
                code == 0 and db.list_tasks(wc, project="PA") == []
                and db.get_session(wc, "a1") is None)
    ok &= check("wipe --all leaves other project",
                len(db.list_tasks(wc, project="PB")) == 1
                and db.get_session(wc, "b1") is not None)
    wc.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_cli.py` -> FAIL (`wipe` unknown verb -> exit 2).

- [ ] **Step 3: Implement `cmd_wipe` in `iterm/cli.py`** (after `cmd_clean`)

```python
def cmd_wipe(args) -> int:
    import swarm
    conn = db.connect()
    if args.all:
        if not args.project:
            return _err("--all requires --project (refusing to wipe every "
                        "project at once)")
        nt = len(db.list_tasks(conn, project=args.project))
        ns = len(db.list_sessions(conn, project=args.project))
        nm = len(db.message_history(conn, project=args.project, limit=10**9))
        print(swarm.wipe_plan_text([], project_all=(nt, ns, nm)))
        if args.dry_run:
            return 0
        if not (nt or ns or nm):
            return 0
        if not args.yes and not _confirm(
                f"permanently DELETE all of project '{args.project}' "
                f"({nt} tasks + {ns} sessions + {nm} messages)?"):
            print("aborted.")
            return 0
        db.wipe_project(conn, args.project)
        print(f"wiped project '{args.project}'.")
        return 0

    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.wipe_candidates(sessions, tasks, names=names)
    print(swarm.wipe_plan_text(cands))
    for w in swarm.wipe_blocker_warnings(cands, tasks):
        print("  " + w)
    if not cands or args.dry_run:
        return 0
    total_tasks = sum(len(c["task_ids"]) for c in cands)
    if not args.yes and not _confirm(
            f"permanently DELETE {total_tasks} task(s) + {len(cands)} "
            f"session(s)?"):
        print("aborted.")
        return 0
    for c in cands:
        db.delete_tasks_for_owner(conn, c["name"])
        db.delete_undelivered_to(conn, c["name"])
        db.delete_session(conn, c["name"])
    print(f"wiped {len(cands)} session(s).")
    return 0
```

Parser (before `return p`):

```python
    wp = sub.add_parser("wipe", help="DELETE dead sessions' tasks (or a whole "
                                     "project with --all)")
    wp.add_argument("names", nargs="*", help="specific closed sessions to wipe "
                    "(default: all closed sessions)")
    wp.add_argument("--project", default=None)
    wp.add_argument("--all", action="store_true",
                    help="with --project: delete the ENTIRE project "
                         "(all tasks/sessions/messages, even live)")
    wp.add_argument("--yes", action="store_true")
    wp.add_argument("--dry-run", dest="dry_run", action="store_true")
    wp.set_defaults(fn=cmd_wipe)
```

`bin/relay`: add `wipe` to the dispatch `case` verb list and the `-h` header
verbs line (`...clean|restore|wipe`). The `-h` line count is unchanged (no new
header line), so the `sed -n '2,18p'` range stays.

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_cli.py` then `./test/run.sh` -> pass. Also `bin/relay -h | grep wipe`.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py bin/relay iterm/test_cli.py
git commit -m "swarm: relay wipe - delete dead sessions' work or a whole project"
```

---

### Task 4: TUI - W key + hint

**Files:**
- Modify: `iterm/app.py`

**Interfaces:**
- Consumes: `watcher.orphan_count`.
- Produces: `W` binding + `action_wipe` (orphaned scope, two-press confirm, shells `relay wipe --yes`); subtitle hint mentions W.

- [ ] **Step 1: Implement (UI-only; validated by test_app render + live)**

BINDINGS - after the `R` binding:

```python
        Binding("W", "wipe", "Wipe orphaned", show=True),
```

`__init__` - after `self._restore_armed = False`:

```python
        self._wipe_armed = False
```

Subtitle hint - in `_refresh`, change the orphan hint line to also mention W:

```python
        if orphans:
            hint = (f"  [#ff5555]· {orphans} task-owner(s) dead - R restore, "
                    f"W wipe, or 'relay clean'[/]")
```

Action (near `action_restore`):

```python
    def action_wipe(self) -> None:
        if not getattr(self.watcher, "orphan_count", 0):
            return
        if not self._wipe_armed:
            self._wipe_armed = True
            self.set_timer(3.0, lambda: setattr(self, "_wipe_armed", False))
            self.query_one(Log).write_line(
                "wipe: press W again within 3s to DELETE orphaned work")
            return
        self._wipe_armed = False
        here = os.path.dirname(os.path.abspath(__file__))
        relay_bin = os.path.join(here, "..", "bin", "relay")
        try:
            subprocess.Popen([relay_bin, "wipe", "--yes"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.query_one(Log).write_line("wipe: deleting orphaned work...")
        except Exception as e:
            self.query_one(Log).write_line(f"wipe failed: {e}")
```

- [ ] **Step 2: Run the suite** (test_app drives the real app, validating the binding loads)

Run: `./test/run.sh` -> `ALL SUITES PASSED`; plus `python3 -c "import sys; sys.path.insert(0,'iterm'); import app"` clean.

- [ ] **Step 3: Live check (deferred to human)** - report only: close a worker tab, see the hint mention W, press W twice, orphaned work deleted.

- [ ] **Step 4: Commit**

```bash
git add iterm/app.py
git commit -m "TUI: W key to wipe orphaned work (two-press confirm)"
```

---

### Task 5: README + final sweep

**Files:**
- Modify: `README.md`, `docs/specs/2026-07-16-swarm-wipe-design.md`

- [ ] **Step 1: README** - in the "Recovering abandoned work" subsection, add `relay wipe`: the delete-counterpart to clean (orphaned scope deletes dead sessions' tasks + rows; `--project P --all` nukes a whole project; plan + confirm + `--dry-run`; the `W` TUI key, orphaned only). State the disposition trio plainly: restore = continue, clean = reset to todo, wipe = delete. Note the blocker warning. No em-dash.

- [ ] **Step 2: Spec status** - change `**Status:** Approved for planning` to `**Status:** Implemented (see docs/plans/2026-07-16-swarm-wipe.md)`.

- [ ] **Step 3: Final sweep**

```bash
./test/run.sh                                    # ALL SUITES PASSED
grep -rn $'\u2014' iterm/ README.md docs/specs/2026-07-16-swarm-wipe-design.md || echo "no em-dashes"
python3 - <<'EOF'
import sys; sys.path.insert(0, "iterm")
import swarm
S=[{"name":"d","closed_at":9.0}]; T=[{"id":1,"owner":"d","state":"doing","blocked_by":""}]
print(swarm.wipe_plan_text(swarm.wipe_candidates(S,T)))
print(swarm.wipe_plan_text([], project_all=(3,1,4)))
EOF
```

Expected: suite green, "no em-dashes", a wipe plan naming `session d` and the `--all` totals form.

Live end-to-end (HUMAN, deferred - list in report, do not run): a real closed worker owning tasks; `relay wipe --dry-run` then `relay wipe` deletes its tasks + row; `relay wipe --project X --all` empties X; the `W` key from the TUI.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/specs/2026-07-16-swarm-wipe-design.md
git commit -m "docs: document relay wipe, mark spec implemented"
```
