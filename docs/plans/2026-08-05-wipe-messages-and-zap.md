# Wipe Carries Messages + Zap Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Session-scoped `relay wipe` (and `clean`) delete the wiped sessions' messages too, and a new TUI `Z Z` shortcut nukes the whole project.

**Architecture:** Three thin layers, matching the existing wipe feature: pure
SQL helpers in `iterm/db.py`, plan-text rendering in `iterm/swarm.py`, verbs
in `iterm/cli.py`, and a TUI binding in `iterm/app.py` that shells out to the
existing `relay wipe --project <p> --all --yes` path. Spec:
`docs/specs/2026-08-05-wipe-messages-and-zap-design.md`.

**Tech Stack:** Python 3 stdlib + sqlite3; Textual TUI; the repo's own
`check()`-style test runners (NO pytest - each `iterm/test_*.py` has a
`__main__` runner; the whole suite is `./test/run.sh`).

## Global Constraints

- NEVER use the em-dash character (U+2014) anywhere; plain ASCII hyphen only.
- No new dependencies. Follow the surrounding code's comment style: comments
  state constraints, not what the next line does.
- Tests are `check("label", cond)` accumulations into `ok`, run via
  `python3 iterm/test_<x>.py`; add to the existing `run()` bodies.
- Messages in a still-OPEN thread are live state and are never deleted
  (matches `prune_threads`); only wipe non-thread posts and posts of
  closed threads.
- Commit messages: conventional-commit style matching `git log` (e.g.
  `feat(db): ...`), no Co-Authored-By trailer.

---

### Task 1: db helpers - delete_messages_for, count_messages_for, list_projects

**Files:**
- Modify: `iterm/db.py` (insert after `delete_undelivered_to`, ~line 841)
- Test: `iterm/test_db.py` (append checks inside `run()` before the final
  summary/return)

**Interfaces:**
- Produces: `db.delete_messages_for(conn, name) -> int` (rows deleted),
  `db.count_messages_for(conn, name) -> int` (same WHERE clause),
  `db.list_projects(conn) -> list[str]` (distinct non-empty projects across
  sessions, tasks, messages, sorted).
- Consumes: existing schema; `threads.state` where `'open'` means live.

- [ ] **Step 1: Write the failing tests**

Append to `run()` in `iterm/test_db.py` (before the suite's final result
lines), using the module's existing `check`/`ok` pattern and a fresh DB:

```python
    # --- delete_messages_for / count_messages_for / list_projects -----------
    mp = _tmpdb()
    mc = db.connect(mp)
    db.register(mc, "ghost", "S-G", "worker", "p1")
    db.register(mc, "alive", "S-A", "worker", "p1")
    m1 = db.queue_message(mc, "ghost", "alive", "sent, delivered", "p1")
    db.mark_delivered(mc, m1)
    db.queue_message(mc, "ghost", "alive", "sent, still queued", "p1")
    m3 = db.queue_message(mc, "alive", "ghost", "received, delivered", "p1")
    db.mark_delivered(mc, m3)
    db.queue_message(mc, "alive", "human", "bystander mail", "p1")
    tid_open = db.create_thread(mc, "open topic", "ghost", ["alive"], "p1")
    db.queue_message(mc, "ghost", "alive", "open thread post", "p1",
                     thread_id=tid_open)
    tid_done = db.create_thread(mc, "done topic", "ghost", ["alive"], "p1")
    db.queue_message(mc, "ghost", "alive", "closed thread post", "p1",
                     thread_id=tid_done)
    db.close_thread(mc, tid_done, "agreed", "settled")
    ok &= check("count_messages_for counts sent+received, skips open thread",
                db.count_messages_for(mc, "ghost") == 4)
    n_del = db.delete_messages_for(mc, "ghost")
    ok &= check("delete_messages_for deletes queued+delivered, both ways",
                n_del == 4)
    left = [r["body"] for r in mc.execute(
        "SELECT body FROM messages ORDER BY id")]
    ok &= check("open-thread post survives, bystander mail survives",
                left == ["bystander mail", "open thread post"])
    ok &= check("list_projects unions the three tables, skips ''",
                db.list_projects(mc) == ["p1"])
    db.queue_message(mc, "x", "y", "other project", "p2")
    db.add_task(mc, "t", project="p3")
    ok &= check("list_projects sees message- and task-only projects, sorted",
                db.list_projects(mc) == ["p1", "p2", "p3"])
    mc.close()
```

Note: `db.add_task(conn, text, project=...)` exists (used elsewhere in this
suite); copy its call shape from the file if the signature differs.

- [ ] **Step 2: Run to verify the new checks fail**

Run: `python3 iterm/test_db.py`
Expected: crash with `AttributeError: module 'db' has no attribute
'count_messages_for'` (a missing function aborts the runner - that is this
suite's failure mode for new API).

- [ ] **Step 3: Implement the three helpers**

In `iterm/db.py`, directly after `delete_undelivered_to` (~line 841):

```python
# Sent or received, queued or delivered - a wiped session's whole voice.
# Open-thread posts are the one carve-out: open discussions are live state
# (same rule as prune_threads), and deleting one dead participant's posts
# would hole out a transcript the survivors can still read. They age out
# via prune_messages once the thread closes.
_MESSAGES_FOR_WHERE = (
    "(from_name=? OR to_name=?) AND (thread_id IS NULL OR thread_id NOT IN "
    "(SELECT id FROM threads WHERE state='open'))")


def count_messages_for(conn, name: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {_MESSAGES_FOR_WHERE}",
        (name, name)).fetchone()[0]


def delete_messages_for(conn, name: str) -> int:
    cur = conn.execute(
        f"DELETE FROM messages WHERE {_MESSAGES_FOR_WHERE}", (name, name))
    conn.commit()
    return cur.rowcount


def list_projects(conn) -> list:
    """Every project with any state at all - a zap target must not miss a
    project that exists only as tasks or only as transcript."""
    rows = conn.execute(
        "SELECT project FROM sessions WHERE project != '' "
        "UNION SELECT project FROM tasks WHERE project != '' "
        "UNION SELECT project FROM messages WHERE project != '' "
        "ORDER BY 1").fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run to verify the checks pass**

Run: `python3 iterm/test_db.py`
Expected: all lines `OK`, suite exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): delete/count a session's messages, list projects"
```

---

### Task 2: wipe plan text shows message counts

**Files:**
- Modify: `iterm/swarm.py:599-617` (`wipe_plan_text`)
- Test: `iterm/test_swarm.py` (append checks inside `run()`)

**Interfaces:**
- Consumes: candidate dicts from `swarm.wipe_candidates` optionally carrying
  a `msg_count` int (stamped by the CLI in Task 3).
- Produces: per-candidate line `delete N task(s), K message(s), session X`
  when `msg_count` is present; unchanged line when absent (existing tests
  keep passing).

- [ ] **Step 1: Write the failing test**

Append to `run()` in `iterm/test_swarm.py`:

```python
    # wipe_plan_text: msg_count renders when stamped, absent stays terse
    cands = [{"name": "g", "task_ids": [1, 2], "workdir": "",
              "worktree_repo": "", "msg_count": 3}]
    ok &= check("wipe plan shows message count when stamped",
                "delete 2 task(s), 3 message(s), session g"
                in swarm.wipe_plan_text(cands))
    cands[0].pop("msg_count")
    ok &= check("wipe plan omits messages when not stamped",
                "delete 2 task(s), session g" in swarm.wipe_plan_text(cands))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_swarm.py`
Expected: `FAIL  wipe plan shows message count when stamped` (the other new
check passes - current format).

- [ ] **Step 3: Implement**

In `swarm.wipe_plan_text`, replace the per-candidate line:

```python
    for c in cands:
        mc = c.get("msg_count")
        msg = f"{mc} message(s), " if mc is not None else ""
        lines.append(f"  delete {len(c['task_ids'])} task(s), {msg}"
                     f"session {c['name']}")
```

(rest of the function unchanged)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_swarm.py`
Expected: all `OK`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): wipe plan lines carry the message count"
```

---

### Task 3: wipe and clean delete the sessions' messages

**Files:**
- Modify: `iterm/cli.py:1728-1745` (`cmd_clean`), `iterm/cli.py:1797-1834`
  (`cmd_wipe`, session-scoped path only - the `--all` path already deletes
  messages via `db.wipe_project`)
- Test: `iterm/test_cli.py` (append checks inside `run()`)

**Interfaces:**
- Consumes: `db.delete_messages_for(conn, name)`,
  `db.count_messages_for(conn, name)` (Task 1); `msg_count` rendering in
  `swarm.wipe_plan_text` (Task 2).
- Produces: `relay wipe` / `relay clean` behavior change; confirm prompts
  `permanently DELETE N task(s) + M session(s) + K message(s)?` and
  `clean M session(s) + DELETE their K message(s)?`.

- [ ] **Step 1: Write the failing tests**

Append to `run()` in `iterm/test_cli.py`, following the suite's existing
wipe-test style (`run_cli(...)` helper, distinct project names, distinct
iTerm ids per session):

```python
    # --- wipe/clean delete the wiped sessions' messages ---------------------
    conn = db.connect()
    db.register(conn, "wm-ghost", "WM-G", "worker", "WMP")
    db.register(conn, "wm-live", "WM-L", "worker", "WMP")
    d1 = db.queue_message(conn, "wm-ghost", "wm-live", "old chatter", "WMP")
    db.mark_delivered(conn, d1)
    db.queue_message(conn, "wm-ghost", "wm-live", "ghost mail queued", "WMP")
    db.queue_message(conn, "wm-live", "human", "live escalation", "WMP")
    db.mark_closed(conn, "wm-ghost", 1.0)
    code, out, _ = run_cli("wipe", "--project", "WMP", "--dry-run",
                           iterm_id="w0t0p0:WM-X")
    ok &= check("wipe plan counts the ghost's messages",
                code == 0 and "2 message(s), session wm-ghost" in out)
    code, out, _ = run_cli("wipe", "--project", "WMP", "--yes",
                           iterm_id="w0t0p0:WM-X")
    left = [r["body"] for r in conn.execute(
        "SELECT body FROM messages WHERE project='WMP' ORDER BY id")]
    ok &= check("wipe deletes ghost's delivered + queued mail, keeps live's",
                code == 0 and left == ["live escalation"])

    db.register(conn, "cl-ghost", "CL-G", "worker", "CLP")
    q = db.queue_message(conn, "cl-ghost", "human", "stale", "CLP")
    db.mark_delivered(conn, q)
    db.mark_closed(conn, "cl-ghost", 1.0)
    code, out, _ = run_cli("clean", "--project", "CLP", "--yes",
                           iterm_id="w0t0p0:CL-X")
    n_left = conn.execute("SELECT COUNT(*) FROM messages "
                          "WHERE project='CLP'").fetchone()[0]
    ok &= check("clean deletes the cleaned session's messages",
                code == 0 and n_left == 0)
```

Note: this suite points `db` at a temp file near the top of the file; reuse
whatever connection/setup pattern the surrounding wipe tests use verbatim.
`cmd_clean` has no `names` positional - check its parser before writing the
`run_cli("clean", ...)` line and drop `--project` if unsupported (add the
messages assertion to however clean is already exercised).

- [ ] **Step 2: Run to verify the new checks fail**

Run: `python3 iterm/test_cli.py`
Expected: `FAIL  wipe plan counts the ghost's messages` and
`FAIL  wipe deletes ghost's delivered + queued mail...` (delivered `old
chatter` survives today), `FAIL  clean deletes...`.

- [ ] **Step 3: Implement**

In `cmd_wipe`'s session-scoped path (after the worktree_action stamping loop,
before `print(swarm.wipe_plan_text(cands))`):

```python
    for c in cands:
        c["msg_count"] = db.count_messages_for(conn, c["name"])
```

Update the confirm to include messages:

```python
    total_tasks = sum(len(c["task_ids"]) for c in cands)
    total_msgs = sum(c["msg_count"] for c in cands)
    if not args.yes and not _confirm(
            f"permanently DELETE {total_tasks} task(s) + {len(cands)} "
            f"session(s) + {total_msgs} message(s)?"):
```

And in the apply loop swap the message delete:

```python
        db.delete_tasks_by_ids(conn, c["task_ids"])
        db.delete_messages_for(conn, c["name"])
        db.delete_session(conn, c["name"])
```

In `cmd_clean`, same swap plus an honest confirm:

```python
    total_msgs = sum(db.count_messages_for(conn, c["name"]) for c in cands)
    if not args.yes and not _confirm(
            f"clean {len(cands)} session(s) + DELETE their {total_msgs} "
            f"message(s)?"):
        print("aborted.")
        return 0
    for c in cands:
        db.reset_owner_tasks(conn, c["name"])
        db.delete_messages_for(conn, c["name"])
        db.delete_session(conn, c["name"])
```

`delete_undelivered_to` stays in the codebase - restore still uses it.

- [ ] **Step 4: Run to verify all cli checks pass**

Run: `python3 iterm/test_cli.py`
Expected: all `OK`, exit 0. If a pre-existing check asserted the old confirm
or plan wording, update that check's expected string - the behavior change
is the point of this task.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py
git commit -m "feat(cli): wipe and clean take the session's messages with it"
```

---

### Task 4: TUI Z Z - zap the whole project

**Files:**
- Modify: `iterm/app.py` - BINDINGS (~line 872), `__init__` (~line 911),
  `KEYBAR` (~line 135), `help_text()` (~line 626), the W no-orphans hint
  (~line 1988), `action_wipe`'s section, `_shell_verb` (~line 2004)
- Test: `iterm/test_app.py` (append checks inside `run()`)

**Interfaces:**
- Consumes: `db.list_projects` (Task 1) via the module's existing `swarmdb`
  import alias; existing `db.list_tasks` / `db.list_sessions` /
  `db.message_history` project filters; `relay wipe --project <p> --all
  --yes` CLI path.
- Produces: `Binding("Z", "zap", ...)`, `RelayApp.action_zap`,
  `_shell_verb(verb, doing, extra=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `run()` in `iterm/test_app.py` (module alias `appmod`, matching
the file's existing KEYBAR/help checks):

```python
    chk("KEYBAR advertises zap", "Z×2" in appmod.KEYBAR
        and "zap" in appmod.KEYBAR.lower())
    chk("help covers zap", "zap" in appmod.help_text().lower())
    chk("RelayApp binds Z to zap",
        any(getattr(b, "key", None) == "Z"
            and getattr(b, "action", "") == "zap"
            for b in appmod.RelayApp.BINDINGS))
    chk("action_zap exists", hasattr(appmod.RelayApp, "action_zap"))
    chk("W hint points at Z for a whole-project clear",
        "Z" in __import__("inspect").getsource(appmod.RelayApp.action_wipe))
```

(Use the suite's actual check-function name - it may be `chk` or `check`;
copy the neighbors.)

- [ ] **Step 2: Run to verify the new checks fail**

Run: `python3 iterm/test_app.py`
Expected: the five new lines FAIL, everything else OK.

- [ ] **Step 3: Implement**

1. BINDINGS, next to W: `Binding("Z", "zap", "Zap project", show=True),`
2. `__init__`, next to `self._wipe_armed = False`: `self._zap_armed = False`
3. KEYBAR second row, after `("W×2", "wipe")`: add `("Z×2", "zap")`
4. `help_text()`, after the `W W` row:
   `row("Z Z", "ZAP the whole project - ALL tasks+sessions+messages (double-press confirms)"),`
5. W no-orphans hint (app.py:1988) becomes:
   `"wipe: nothing orphaned - W deletes work owned by CLOSED sessions. "`
   `"To clear a whole project press Z twice."`
6. `_shell_verb` gains extra argv:

```python
    def _shell_verb(self, verb: str, doing: str, extra=None) -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        relay_bin = os.path.join(here, "..", "bin", "relay")
        try:
            subprocess.Popen([relay_bin, verb, *(extra or []), "--yes"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.query_one(Log).write_line(f"{verb}: {doing}...")
        except Exception as e:
            self.query_one(Log).write_line(f"{verb} failed: {e}")
```

7. New action after `action_wipe`:

```python
    # --- zap (delete a WHOLE project - tasks + sessions + messages) ------
    def action_zap(self) -> None:
        if self._any_overlay_open():
            return
        log = self.query_one(Log)
        try:
            if self._swarm_db is None:
                self._swarm_db = swarmdb.connect()
            projects = swarmdb.list_projects(self._swarm_db)
        except Exception as e:
            log.write_line(f"zap: swarm db unavailable: {e}")
            return
        if not projects:
            log.write_line("zap: no projects - nothing to delete")
            return
        if len(projects) > 1:
            # Guessing a target for a permanent delete is worse than a
            # shell round-trip; the CLI form names the project explicitly.
            log.write_line(
                f"zap: several projects ({', '.join(projects)}) - use: "
                f"relay wipe --project <p> --all")
            return
        p = projects[0]
        if not self._zap_armed:
            self._zap_armed = True
            self.set_timer(self._CONFIRM_WINDOW,
                           lambda: setattr(self, "_zap_armed", False))
            nt = len(swarmdb.list_tasks(self._swarm_db, project=p))
            ns = len(swarmdb.list_sessions(self._swarm_db, project=p))
            nm = len(swarmdb.message_history(self._swarm_db, project=p,
                                             limit=10**9))
            log.write_line(
                f"zap ARMED: press Z again to DELETE ALL of project '{p}' "
                f"({nt} tasks + {ns} sessions + {nm} messages, auto-cancels "
                f"in {int(self._CONFIRM_WINDOW)}s)")
            return
        self._zap_armed = False
        self._shell_verb("wipe", f"deleting ALL of project '{p}'",
                         extra=["--project", p, "--all"])
```

Note the module imports `db` as `swarmdb` - check the import line at the top
of app.py and use whatever alias it actually has. Dirty worktrees are kept
by the CLI path itself; nothing extra to do here.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_app.py`
Expected: all `OK`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): Z zaps the whole project, double-press guarded"
```

---

### Task 5: docs + full suite

**Files:**
- Modify: `README.md` (keybar line ~32, key table ~231, wipe section
  ~779-835), `docs/specs/2026-08-05-wipe-messages-and-zap-design.md`
  (Status line)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update README**

- Keys line (~32): add `Z×2 zap` after `W×2 wipe`.
- Key table (~231): add a row after `W W`:
  `| `Z` `Z` | **Press twice:** ZAP the whole project - all tasks, sessions and messages. Refuses to guess when several projects exist |`
- Wipe section (~779 and the `relay wipe` block ~811): state that wiping a
  session also deletes every message it sent or received (queued or
  delivered), except posts in still-open discussions; note `clean` does the
  same.

- [ ] **Step 2: Flip the spec status**

In `docs/specs/2026-08-05-wipe-messages-and-zap-design.md` set
`**Status:** Implemented (see docs/plans/2026-08-05-wipe-messages-and-zap.md)`.

- [ ] **Step 3: Run the whole suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/specs/2026-08-05-wipe-messages-and-zap-design.md
git commit -m "docs: wipe deletes the session's messages; Z zaps the project"
```
