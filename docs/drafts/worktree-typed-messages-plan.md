# Worktree Spawn + Typed Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `relay spawn --worktree` (git-worktree-isolated workers) and typed/broadcast messages (`relay send --kind`, `--all`, escalation pings) per `docs/drafts/worktree-typed-messages-design.md`.

**Architecture:** All swarm state stays in the SQLite bus (`iterm/db.py`); pure decision/rendering logic stays in `iterm/swarm.py` (no sqlite/iterm2 imports); CLI verbs in `iterm/cli.py`; the watcher (`iterm/watcher.py`) is the only component that touches iTerm2. Git operations (worktree add/remove, dirty check) live in `iterm/cli.py` as small subprocess helpers, mirroring the existing `_git` pattern.

**Tech Stack:** Python 3 stdlib only (sqlite3, subprocess, argparse). No pytest - each suite is a `__main__` runner with a `check(msg, cond)` helper; run all via `./test/run.sh`.

## Global Constraints

- No em-dash characters anywhere (code, docs, commits) - plain ASCII hyphens only.
- No new dependencies; pure-stdlib modules stay pure (swarm.py: no sqlite/iterm2 imports; db.py: no iterm2 imports).
- Commit messages: conventional style like recent history (`feat: ...`, `docs: ...`), NO Co-Authored-By trailer.
- `--dry-run` must never mutate anything; wipe must never delete uncommitted work.
- Worktrees live next to the repo (`<parent>/<reponame>-<name>`), branch `relay/<name>`. Never under `~/.relay`.
- Message kinds: known set `info|done|blocked|escalation|wake`; `wake` reserved for relay-generated messages; custom kinds allowed as single lowercase tokens.
- Tests must not require iTerm2 or a live terminal (stub `spawn.spawn_worker`).

---

### Task 1: DB migration v5 (message kind + worktree_repo)

**Files:**
- Modify: `iterm/db.py` (schema at :24-60, `_CURRENT_VERSION`/`_MIGRATIONS` at :87-95, `queue_message` at :238-245, add `set_worktree_repo` near `set_session_context` :196)
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces: `db.MESSAGE_KINDS = ("info", "done", "blocked", "escalation", "wake")`; `db.queue_message(conn, from_name, to_name, body, project="", now=None, kind="info") -> int`; `db.set_worktree_repo(conn, name: str, repo: str) -> bool`; messages rows gain `kind` (TEXT, default `'info'`); sessions rows gain `worktree_repo` (TEXT, default `''`).

- [ ] **Step 1: Write the failing tests**

Append inside `run()` in `iterm/test_db.py`, before its final return, following the file's existing `check(...)` pattern (temp DB files are already set up at module top; reuse the same mechanism the existing migration tests use, or a fresh temp path via `tempfile.mkdtemp()`):

```python
    # --- v5: message kind + worktree_repo -------------------------------------
    p5 = os.path.join(tempfile.mkdtemp(), "v5.db")
    conn5 = db.connect(p5)
    ok &= check("fresh DB is schema v5",
                conn5.execute("PRAGMA user_version").fetchone()[0] == 5)
    mid = db.queue_message(conn5, "a", "b", "hello")
    row = conn5.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    ok &= check("queue_message defaults kind=info", row["kind"] == "info")
    mid2 = db.queue_message(conn5, "a", "b", "done!", kind="done")
    row2 = conn5.execute("SELECT * FROM messages WHERE id=?", (mid2,)).fetchone()
    ok &= check("queue_message stores explicit kind", row2["kind"] == "done")

    db.register(conn5, "w1", "SID-W1", "worker", "proj")
    ok &= check("worktree_repo defaults empty",
                db.get_session(conn5, "w1")["worktree_repo"] == "")
    ok &= check("set_worktree_repo writes",
                db.set_worktree_repo(conn5, "w1", "/tmp/repo")
                and db.get_session(conn5, "w1")["worktree_repo"] == "/tmp/repo")
    ok &= check("set_worktree_repo unknown name -> False",
                not db.set_worktree_repo(conn5, "ghost", "/tmp/x"))

    # upgrade path: hand-build a v4 DB (no kind / worktree_repo), then connect
    p4 = os.path.join(tempfile.mkdtemp(), "v4.db")
    import sqlite3 as _sq
    old = _sq.connect(p4)
    old.executescript("""
      CREATE TABLE sessions(name TEXT PRIMARY KEY, iterm_session_id TEXT NOT NULL,
        role TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
        status_text TEXT NOT NULL DEFAULT '', registered_at REAL NOT NULL,
        last_seen REAL NOT NULL, arm_request TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '', workdir TEXT NOT NULL DEFAULT '',
        spawn_prompt TEXT NOT NULL DEFAULT '', closed_at REAL NOT NULL DEFAULT 0);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL,
        to_name TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
        delivered_at REAL);
      CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL DEFAULT '', parent_id INTEGER, title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'todo', owner TEXT, spec_path TEXT,
        blocked_by TEXT NOT NULL DEFAULT '', created_by TEXT,
        updated_at REAL NOT NULL);
      PRAGMA user_version = 4;
    """)
    old.commit(); old.close()
    up = db.connect(p4)
    ok &= check("v4 -> v5 migration runs",
                up.execute("PRAGMA user_version").fetchone()[0] == 5)
    cols_m = {r[1] for r in up.execute("PRAGMA table_info(messages)")}
    cols_s = {r[1] for r in up.execute("PRAGMA table_info(sessions)")}
    ok &= check("migration adds kind + worktree_repo",
                "kind" in cols_m and "worktree_repo" in cols_s)
```

If `tempfile` is not already imported in `test_db.py`, add `import tempfile` at the top with the other imports.

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_db.py`
Expected: FAIL lines (fresh DB reports v4, `queue_message` rejects `kind=`, missing `set_worktree_repo` raises -> caught as test failure or traceback).

- [ ] **Step 3: Implement in `iterm/db.py`**

Add after `ARM_REQUEST_MODES` (:22):

```python
# Message kinds with dedicated rendering/behavior. 'wake' is reserved for
# relay-generated wake-ups; custom kinds beyond this set are allowed and
# render plain. Validation lives in the CLI - the DB stores what it is given.
MESSAGE_KINDS = ("info", "done", "blocked", "escalation", "wake")
```

In `_SCHEMA`: add to the `sessions` CREATE (after `closed_at REAL NOT NULL DEFAULT 0`):

```sql
  closed_at REAL NOT NULL DEFAULT 0,
  worktree_repo TEXT NOT NULL DEFAULT ''
```

and to the `messages` CREATE (after `delivered_at REAL`):

```sql
  delivered_at REAL,
  kind TEXT NOT NULL DEFAULT 'info'
```

Bump the version and add the migration (:87-95); also extend the comment above `_migrate` mentioning v5:

```python
_CURRENT_VERSION = 5
_MIGRATIONS = {
    # from_version: (SQL to run, ...)
    1: ("ALTER TABLE sessions ADD COLUMN arm_request TEXT NOT NULL DEFAULT ''",),
    2: ("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT ''",),
    3: ("ALTER TABLE sessions ADD COLUMN workdir TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN spawn_prompt TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN closed_at REAL NOT NULL DEFAULT 0"),
    4: ("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'info'",
        "ALTER TABLE sessions ADD COLUMN worktree_repo TEXT NOT NULL DEFAULT ''"),
}
```

`queue_message` (:238) gains the kind param:

```python
def queue_message(conn, from_name: str, to_name: str, body: str,
                  project: str = "", now: Optional[float] = None,
                  kind: str = "info") -> int:
    cur = conn.execute(
        """INSERT INTO messages(project, from_name, to_name, body, created_at,
                                kind)
           VALUES(?,?,?,?,?,?)""",
        (project, from_name, to_name, body, _now(now), kind or "info"))
    conn.commit()
    return cur.lastrowid
```

Add after `set_session_context` (:204):

```python
def set_worktree_repo(conn, name: str, repo: str) -> bool:
    """Record that this session's workdir is a relay-created git worktree of
    `repo`, so wipe can offer to remove it (only when clean)."""
    cur = conn.execute("UPDATE sessions SET worktree_repo=? WHERE name=?",
                       (repo, name))
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 iterm/test_db.py` -> all OK. Then `./test/run.sh` -> no regressions.

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): schema v5 - message kind + session worktree_repo"
```

---

### Task 2: `relay send --kind` and `--all` broadcast

**Files:**
- Modify: `iterm/cli.py` (`cmd_send` :104-115, `send` parser :527-530, add `import re` to the imports at :18-21)
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.queue_message(..., kind=)`, `db.list_sessions(conn, project)` (Task 1).
- Produces: CLI surface `relay send <name> "<body>" [--kind K]` and `relay send --all --project <p> "<body>" [--kind K]`; module-level `cli._KIND_RE`.

- [ ] **Step 1: Write the failing tests**

Append inside `run()` in `iterm/test_cli.py` (after the existing send/inbox block ~:90; sessions `coord` at `w0t0p0:CO-ID` and `bff-worker` at `w0t1p0:BFF-ID`, project `webshop`, already exist):

```python
    # --- typed messages -------------------------------------------------------
    code, _, _ = run_cli("send", "bff-worker", "branch ready", "--kind", "done",
                         iterm_id="w0t0p0:CO-ID")
    row = db.undelivered(conn, "bff-worker")[0]
    ok &= check("send --kind stored", code == 0 and row["kind"] == "done")
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    code, _, err = run_cli("send", "bff-worker", "x", "--kind", "wake",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("kind wake reserved", code == 1 and "reserved" in err)
    code, _, err = run_cli("send", "bff-worker", "x", "--kind", "Not Valid",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("bad kind rejected", code == 1 and "lowercase" in err)
    code, _, _ = run_cli("send", "bff-worker", "y", "--kind", "review-me",
                         iterm_id="w0t0p0:CO-ID")
    ok &= check("custom kind allowed", code == 0
                and db.undelivered(conn, "bff-worker")[0]["kind"] == "review-me")
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    # --- broadcast ------------------------------------------------------------
    code, _, err = run_cli("send", "--all", "hello", iterm_id="w0t0p0:CO-ID")
    ok &= check("--all needs --project", code == 1 and "--project" in err)
    code, _, err = run_cli("send", "--all", "--project", "webshop", "a", "b",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("--all with two positionals rejected",
                code == 1 and "only the message body" in err)
    code, out, _ = run_cli("send", "--all", "--project", "webshop",
                           "freeze: rebasing", iterm_id="w0t0p0:CO-ID")
    ok &= check("broadcast queues to others, not sender", code == 0
                and len(db.undelivered(conn, "bff-worker")) == 1
                and db.undelivered(conn, "coord") == [])
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain
```

Note for the implementer: if earlier tests in the file registered more `webshop` sessions than `coord` + `bff-worker` by this point, adjust the recipient-count assertion to match (the invariants that matter: sender excluded, all other live project members included).

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_cli.py`
Expected: FAIL (`--kind`/`--all` are unknown arguments -> argparse exit 2).

- [ ] **Step 3: Implement in `iterm/cli.py`**

Add `import re` to the stdlib imports. Add above `cmd_send`:

```python
# Custom message kinds are allowed but kept machine-friendly: one short
# lowercase token. Known kinds (db.MESSAGE_KINDS) get dedicated rendering.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
```

Replace `cmd_send` (:104-115):

```python
def cmd_send(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    kind = args.kind or "info"
    if kind == "wake":
        return _err("kind 'wake' is reserved for relay's automatic wake-ups")
    if not _KIND_RE.match(kind):
        return _err(f"--kind must be one short lowercase token "
                    f"(a-z, 0-9, -, _), got {kind!r}")
    if args.all:
        if args.body is not None:
            return _err("with --all, pass only the message body")
        if args.to is None:
            return _err("message body required")
        if not args.project:
            return _err("--all requires --project")
        body = args.to
        targets = [s for s in db.list_sessions(conn, args.project)
                   if s["name"] != me["name"] and not s["closed_at"]]
        if not targets:
            return _err(f"no live sessions in project '{args.project}'")
        for s in targets:
            db.queue_message(conn, me["name"], s["name"], body,
                             args.project, kind=kind)
        print(f"queued for {len(targets)} session(s): "
              + ", ".join(s["name"] for s in targets))
        return 0
    if args.to is None or args.body is None:
        return _err('usage: relay send <name> "<body>"  or  '
                    'relay send --all --project <p> "<body>"')
    if db.get_session(conn, args.to) is None:
        return _err(f"unknown recipient '{args.to}' - relay msgs shows known "
                    f"names; sessions register themselves first")
    db.queue_message(conn, me["name"], args.to, args.body, me["project"],
                     kind=kind)
    print(f"queued for {args.to} (delivered when their session is idle "
          f"and the relay TUI is running)")
    return 0
```

Replace the `send` parser block (:527-530):

```python
    sd = sub.add_parser("send", help="queue a message to a named session")
    sd.add_argument("to", nargs="?", default=None,
                    help="recipient name (omit with --all)")
    sd.add_argument("body", nargs="?", default=None)
    sd.add_argument("--kind", default="info",
                    help="info|done|blocked|escalation or a custom lowercase "
                         "token ('wake' is reserved)")
    sd.add_argument("--all", action="store_true",
                    help="broadcast to every live session in --project "
                         "(except me)")
    sd.add_argument("--project", default=None)
    sd.set_defaults(fn=cmd_send)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 iterm/test_cli.py` -> all OK. Then `./test/run.sh`.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py
git commit -m "feat(cli): relay send --kind and --all broadcast"
```

---

### Task 3: kind-aware delivery, listings, and wake messages

**Files:**
- Modify: `iterm/swarm.py` (`delivery_text` :57-67, `render_swarm` messages loop :342-348)
- Modify: `iterm/cli.py` (`cmd_inbox` :118-131, `cmd_msgs` :134-145, `cmd_task_add` wake :175-178, `cmd_task_update` wake :190-196)
- Modify: `iterm/watcher.py` (`_deliver` :560)
- Test: `iterm/test_swarm.py`, `iterm/test_cli.py`

**Interfaces:**
- Produces: `swarm.delivery_text(from_name, body, kind="info") -> str`; `swarm.kind_of(msg) -> str` (tolerates rows/dicts without a kind key -> `"info"`). Delivery prefix: `[relay msg from X]` for info, `[relay <kind> from X]` otherwise.

- [ ] **Step 1: Write the failing tests**

`iterm/test_swarm.py`, inside `run()` following its `check` pattern:

```python
    ok &= check("delivery_text info unchanged",
                swarm.delivery_text("coord", "hi") == "[relay msg from coord] hi")
    ok &= check("delivery_text carries kind",
                swarm.delivery_text("bff", "done", "done")
                == "[relay done from bff] done")
    ok &= check("kind_of tolerates missing key",
                swarm.kind_of({"id": 1, "body": "x"}) == "info")
    ok &= check("kind_of reads kind",
                swarm.kind_of({"id": 1, "kind": "blocked"}) == "blocked")
    fed = swarm.render_swarm(
        [], [],
        [{"from_name": "a", "to_name": "b", "body": "hi", "delivered_at": 1,
          "kind": "escalation"}], now=0.0)
    ok &= check("feed tags non-info kind", "[escalation]" in fed)
    fed2 = swarm.render_swarm(
        [], [],
        [{"from_name": "a", "to_name": "b", "body": "hi", "delivered_at": 1,
          "kind": "info"}], now=0.0)
    ok &= check("feed leaves info untagged", "[info]" not in fed2)
```

(If `render_swarm([], [], ...)` returns the NO SWARM YET teaching screen, register one session dict in the fixtures instead - e.g. `[{"name": "a", "role": "worker", "project": "", "status_text": ""}]` - the messages section renders regardless.)

`iterm/test_cli.py`, after the Task 2 block:

```python
    # wake messages are kind='wake'; msgs shows kinds
    code, _, _ = run_cli("task", "add", "wired task", "--owner", "bff-worker",
                         iterm_id="w0t0p0:CO-ID")
    wake = db.undelivered(conn, "bff-worker")[0]
    ok &= check("assignment wake has kind=wake", wake["kind"] == "wake")
    code, out, _ = run_cli("msgs", "--project", "webshop")
    ok &= check("msgs shows kind tag", "[wake]" in out)
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_swarm.py && python3 iterm/test_cli.py`
Expected: FAIL (`delivery_text` takes 2 args, no `kind_of`, no feed tag, wake kind is `info`).

- [ ] **Step 3: Implement**

`iterm/swarm.py` - replace `delivery_text` and add `kind_of` right after it:

```python
def delivery_text(from_name: str, body: str, kind: str = "info") -> str:
    """The literal text typed into the target session. Newlines flattened so
    the injected turn is one paste + one Enter (bracketed-paste lesson).

    This text is sent as raw keystrokes, so ESC / C0 control bytes (e.g. an
    interrupt sequence) in an attacker-influenceable body would be interpreted
    by the terminal, not typed. Strip everything that isn't printable or a
    plain space after flattening."""
    flat = " ".join(str(body).splitlines())
    flat = "".join(c for c in flat if c.isprintable() or c == " ")
    tag = "msg" if kind in ("", "info") else kind
    return f"[relay {tag} from {from_name}] {flat}"


def kind_of(m) -> str:
    """A message row/dict's kind, defaulting 'info' for pre-v5 rows and plain
    dict fixtures (sqlite Row and dict both support .keys())."""
    try:
        k = m["kind"] if "kind" in m.keys() else ""
    except Exception:
        k = ""
    return k or "info"
```

`render_swarm` messages loop (:342-348) becomes:

```python
    out.append("MESSAGES")
    for m in messages[-8:]:
        q = "" if m["delivered_at"] else "  [queued]"
        k = kind_of(m)
        tag = f"[{k}] " if k != "info" else ""
        out.append(f"  {m['from_name']} -> {m['to_name']}: "
                   f"{tag}{_clip(m['body'], width - 30)}{q}")
```

`iterm/watcher.py` `_deliver` (:560):

```python
        text = swarm.delivery_text(m["from_name"], m["body"], swarm.kind_of(m))
```

`iterm/cli.py`:
- `cmd_task_add` (:177): `db.queue_message(conn, "relay", args.owner, swarm.wakeup_assignment_body(task), project, kind="wake")`
- `cmd_task_update` (:195): `db.queue_message(conn, "relay", t["owner"], swarm.wakeup_unblocked_body(t), t["project"], kind="wake")`
- `cmd_msgs` line print (:143-144):

```python
    for m in rows:
        tick = "" if m["delivered_at"] else "  [queued]"
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))} "
              f"{m['from_name']} -> {m['to_name']}{tag}: {m['body']}{tick}")
```

- `cmd_inbox` line print (:128-129):

```python
    for m in msgs:
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        print(f"#{m['id']} from {m['from_name']}{tag} "
              f"({_ago(m['created_at'])}): {m['body']}")
        db.mark_delivered(conn, m["id"])
```

- [ ] **Step 4: Run to verify pass**

Run: `./test/run.sh` -> all suites OK (this catches any existing fixture broken by the new rendering).

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/cli.py iterm/watcher.py iterm/test_swarm.py iterm/test_cli.py
git commit -m "feat(swarm): kind-aware delivery prefix, listings, wake kinds"
```

---

### Task 4: escalation messages ping the human

**Files:**
- Modify: `iterm/swarm.py` (new pure selector), `iterm/watcher.py` (`__init__` :136, `start` loop after `_swarm_refresh_registry()` :180, new method near `_check_stale` :594)
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Produces: `swarm.escalation_pings(msgs, already: set) -> list` (pure); watcher fires `notify_mac` once per escalation message id, tracked in `self._escalation_pinged` (in-memory; re-ping after panel restart is accepted per spec).

- [ ] **Step 1: Write the failing test**

`iterm/test_swarm.py`:

```python
    esc = [{"id": 1, "kind": "escalation", "from_name": "w1", "to_name": "c",
            "body": "need creds"},
           {"id": 2, "kind": "info", "from_name": "w1", "to_name": "c",
            "body": "hi"},
           {"id": 3, "kind": "escalation", "from_name": "w2", "to_name": "c",
            "body": "stuck"}]
    ok &= check("escalation_pings picks unpinged escalations",
                [m["id"] for m in swarm.escalation_pings(esc, {1})] == [3])
    ok &= check("escalation_pings empty when all seen",
                swarm.escalation_pings(esc, {1, 3}) == [])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL (`swarm` has no `escalation_pings`).

- [ ] **Step 3: Implement**

`iterm/swarm.py`, after `kind_of`:

```python
def escalation_pings(msgs, already: set) -> list:
    """Queued messages that should ping the human NOW: kind 'escalation' and
    not already pinged. Delivery still waits for the target's idle prompt;
    the ping must not."""
    return [m for m in msgs
            if kind_of(m) == "escalation" and m["id"] not in already]
```

`iterm/watcher.py` - in `__init__`, after `self._dryrun_delivered: set = set()` (:136):

```python
        self._escalation_pinged: set = set()   # msg ids already pinged
```

New method directly above `_check_stale` (:594):

```python
    def _check_escalations(self) -> None:
        """A worker sending --kind escalation is calling for a human. Ping
        (sound + notification) the moment the message is queued - even if the
        target session is busy - once per message. Runs in dry-run too:
        notify is the zero-blast-radius half, same as prompt alerts."""
        try:
            msgs = swarmdb.undelivered(self._swarm_conn())
        except Exception:
            return
        for m in swarm.escalation_pings(msgs, self._escalation_pinged):
            self._escalation_pinged.add(m["id"])
            self._note(f"ESCALATION from {m['from_name']} -> {m['to_name']}: "
                       f"{m['body'][:80]}")
            notify_mac(f"Relay - escalation from {m['from_name']}",
                       m["body"][:120], self.alert_sound)
```

In `start()`, after `self._swarm_refresh_registry()` (:180):

```python
                self._check_escalations()
```

- [ ] **Step 4: Run to verify pass**

Run: `./test/run.sh` -> all OK (watcher has no headless suite; the pure selector carries the logic, the wiring is three lines).

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/watcher.py iterm/test_swarm.py
git commit -m "feat(watcher): escalation-kind messages ping the human immediately"
```

---

### Task 5: `relay spawn --worktree`

**Files:**
- Modify: `iterm/cli.py` (`cmd_spawn` :379-394, `spawn` parser :562-573, new `_worktree_add` helper above `cmd_spawn`)
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.set_worktree_repo` (Task 1).
- Produces: `cli._worktree_add(repo: str, name: str) -> tuple` returning `(worktree_path, None)` on success or `(None, error_message)`; CLI flag `relay spawn ... --dir <repo> --worktree`.

- [ ] **Step 1: Write the failing tests**

`iterm/test_cli.py`. First add a spawn stub near the top of `run()` (before any spawn tests; `cmd_spawn` does `import spawn as spawnmod` at call time, so patching the module attribute sticks):

```python
    import spawn as spawnmod
    spawn_calls = []

    async def _fake_spawn(name, project, prompt, workdir, role="worker",
                          arm="off"):
        spawn_calls.append({"name": name, "workdir": workdir, "arm": arm})
        c = db.connect()
        db.register(c, name, f"FAKE-{name}", role, project)
        db.set_session_context(c, name, workdir, prompt)
        return f"FAKE-{name}"

    spawnmod.spawn_worker = _fake_spawn
```

Then the worktree tests (uses a real temp git repo; `subprocess` import may need adding at top):

```python
    # --- spawn --worktree -----------------------------------------------------
    import subprocess
    repo = os.path.join(tempfile.mkdtemp(), "webshop")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "root"], check=True)
```

Then:

```python
    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("--worktree requires --dir", code == 1 and "--dir" in err)

    nogit = tempfile.mkdtemp()
    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           "--dir", nogit, iterm_id="w0t0p0:CO-ID")
    ok &= check("--worktree needs a git repo", code == 1
                and "not a git repository" in err)

    code, out, _ = run_cli("spawn", "go", "--name", "wt1", "--project",
                           "webshop", "--worktree", "--dir", repo,
                           iterm_id="w0t0p0:CO-ID")
    wt = os.path.join(os.path.dirname(repo), "webshop-wt1")
    ok &= check("worktree created + spawned there", code == 0
                and os.path.isdir(wt) and spawn_calls[-1]["workdir"] == wt)
    branches = subprocess.run(["git", "-C", repo, "branch", "--list",
                               "relay/wt1"], capture_output=True, text=True)
    ok &= check("branch relay/wt1 exists", "relay/wt1" in branches.stdout)
    ok &= check("worktree_repo recorded",
                db.get_session(conn, "wt1")["worktree_repo"] == repo)

    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           "--dir", repo, iterm_id="w0t0p0:CO-ID")
    ok &= check("existing worktree path refused", code == 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_cli.py`
Expected: FAIL (`--worktree` unknown argument -> exit 2).

- [ ] **Step 3: Implement in `iterm/cli.py`**

Above `cmd_spawn`:

```python
def _worktree_add(repo: str, name: str):
    """Create branch relay/<name> and a sibling worktree <repo>-<name> from
    the repo's current HEAD. Returns (worktree_path, None) on success or
    (None, error). The worktree lives NEXT TO the repo, never under ~/.relay -
    relay is a tech the session uses, not a place that owns the work."""
    import subprocess
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"not a git repository: {repo}"
    path = os.path.join(os.path.dirname(repo),
                        f"{os.path.basename(repo)}-{name}")
    if os.path.exists(path):
        return None, (f"worktree path already exists: {path} - pick another "
                      f"--name, or remove it (git -C {repo} worktree remove)")
    r = subprocess.run(["git", "-C", repo, "worktree", "add", path,
                        "-b", f"relay/{name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr.strip() or "git worktree add failed")
    return path, None
```

`cmd_spawn` becomes:

```python
def cmd_spawn(args) -> int:
    import asyncio
    import config as relay_config
    import spawn as spawnmod
    if args.worktree and not args.dir:
        return _err("--worktree requires --dir <repo>")
    workdir = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(workdir):
        return _err(f"workdir not found: {workdir}")
    repo = None
    if args.worktree:
        repo = workdir
        workdir, wt_err = _worktree_add(repo, args.name)
        if wt_err:
            return _err(wt_err)
        print(f"worktree {workdir} (branch relay/{args.name})")
    # --arm beats config [swarm] spawn_arm beats "off".
    arm = args.arm if args.arm is not None else relay_config.load()[0].spawn_arm
    sid = asyncio.run(spawnmod.spawn_worker(
        args.name, args.project or "", args.prompt, workdir, args.role,
        arm=arm))
    if repo:
        db.set_worktree_repo(db.connect(), args.name, repo)
    armed = f", arm={arm}" if arm != "off" else ""
    print(f"spawned '{args.name}' ({args.role}{armed}) in {workdir} "
          f"[session {sid[:8]}]")
    return 0
```

Parser (:562-573) - add after the `--arm` argument:

```python
    sp.add_argument("--worktree", action="store_true",
                    help="create a git worktree of --dir (branch relay/<name>, "
                         "sibling dir <repo>-<name>) and spawn the worker there")
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 iterm/test_cli.py` -> all OK. Then `./test/run.sh`.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py
git commit -m "feat(spawn): --worktree - isolate same-repo workers via git worktrees"
```

---

### Task 6: wipe offers clean-worktree removal

**Files:**
- Modify: `iterm/swarm.py` (`wipe_candidates` :215-225, `wipe_plan_text` :245-256), `iterm/cli.py` (`cmd_wipe` :417-470, two git helpers near `_worktree_add`)
- Test: `iterm/test_swarm.py`, `iterm/test_cli.py`

**Interfaces:**
- Consumes: `worktree_repo` on session rows (Task 1), worktrees created by Task 5.
- Produces: wipe candidates carry `workdir`, `worktree_repo`, and cli-computed `worktree_action` in `(None, "remove", "keep-dirty")`; `cli._worktree_dirty(workdir) -> bool`; `cli._worktree_remove(repo, workdir, name) -> (ok, err)`.

- [ ] **Step 1: Write the failing tests**

`iterm/test_swarm.py`:

```python
    wsess = [{"name": "w1", "closed_at": 5, "workdir": "/tmp/r-w1",
              "worktree_repo": "/tmp/r"}]
    wc = swarm.wipe_candidates(wsess, [])
    ok &= check("wipe candidate carries worktree fields",
                wc[0]["workdir"] == "/tmp/r-w1"
                and wc[0]["worktree_repo"] == "/tmp/r")
    wc[0]["worktree_action"] = "remove"
    ok &= check("wipe plan shows worktree removal",
                "remove worktree /tmp/r-w1" in swarm.wipe_plan_text(wc)
                and "relay/w1" in swarm.wipe_plan_text(wc))
    wc[0]["worktree_action"] = "keep-dirty"
    ok &= check("wipe plan keeps dirty worktree",
                "uncommitted" in swarm.wipe_plan_text(wc))
```

`iterm/test_cli.py` (continues from Task 5's `repo`/`wt` and stubbed spawn; `wt1` exists with a clean worktree):

```python
    # --- wipe removes clean worktrees, keeps dirty ones -----------------------
    # second worktree worker, made dirty
    run_cli("spawn", "go", "--name", "wt2", "--project", "webshop",
            "--worktree", "--dir", repo, iterm_id="w0t0p0:CO-ID")
    wt2 = os.path.join(os.path.dirname(repo), "webshop-wt2")
    with open(os.path.join(wt2, "uncommitted.txt"), "w") as f:
        f.write("wip")
    # both must be CLOSED to be wipe candidates
    import time as _t
    db.mark_closed(conn, "wt1", _t.time())
    db.mark_closed(conn, "wt2", _t.time())

    code, out, _ = run_cli("wipe", "wt1", "wt2", "--project", "webshop",
                           "--dry-run")
    ok &= check("dry-run plans removal + keep", code == 0
                and "remove worktree" in out and "uncommitted" in out)
    ok &= check("dry-run removed nothing",
                os.path.isdir(wt) and os.path.isdir(wt2))

    code, out, _ = run_cli("wipe", "wt1", "wt2", "--project", "webshop",
                           "--yes")
    ok &= check("wipe removed clean worktree", code == 0
                and not os.path.exists(wt))
    ok &= check("wipe kept dirty worktree", os.path.isdir(wt2))
    branches = subprocess.run(["git", "-C", repo, "branch", "--list",
                               "relay/wt1"], capture_output=True, text=True)
    ok &= check("branch relay/wt1 deleted", "relay/wt1" not in branches.stdout)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_swarm.py && python3 iterm/test_cli.py`
Expected: FAIL (candidates lack worktree fields, plan text lacks worktree lines, wipe leaves the clean worktree in place).

- [ ] **Step 3: Implement**

`iterm/swarm.py` - `wipe_candidates` entry dict becomes:

```python
        out.append({"name": s["name"], "task_ids": _all_ids(tasks, s["name"]),
                    "workdir": s.get("workdir", "") if hasattr(s, "get")
                               else s["workdir"],
                    "worktree_repo": s.get("worktree_repo", "")
                               if hasattr(s, "get") else s["worktree_repo"]})
```

(cli passes plain dicts via `dict(r)`, so `.get` covers rows from any schema version and hand-built test fixtures alike; the `hasattr` guard keeps sqlite Row inputs working.)

`wipe_plan_text` per-candidate loop becomes:

```python
    lines = ["WIPE PLAN"]
    for c in cands:
        lines.append(f"  delete {len(c['task_ids'])} task(s), "
                     f"session {c['name']}")
        wa = c.get("worktree_action")
        if wa == "remove":
            lines.append(f"    remove worktree {c['workdir']} "
                         f"+ branch relay/{c['name']}")
        elif wa == "keep-dirty":
            lines.append(f"    KEEP worktree {c['workdir']} - uncommitted "
                         f"changes (relay never deletes unsaved work)")
```

`iterm/cli.py` - two helpers below `_worktree_add`:

```python
def _worktree_dirty(workdir: str) -> bool:
    """True when the worktree has uncommitted/untracked changes - or can't be
    read at all (unreadable counts as dirty: never delete blind)."""
    import subprocess
    r = subprocess.run(["git", "-C", workdir, "status", "--porcelain"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def _worktree_remove(repo: str, workdir: str, name: str):
    """Remove a relay-created worktree + its relay/<name> branch. Branch
    deletion is best-effort (already merged-and-deleted is not an error)."""
    import subprocess
    r = subprocess.run(["git", "-C", repo, "worktree", "remove", workdir],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr.strip() or "git worktree remove failed")
    subprocess.run(["git", "-C", repo, "branch", "-D", f"relay/{name}"],
                   capture_output=True, text=True)
    return True, ""
```

`cmd_wipe` - after `cands = swarm.wipe_candidates(...)` (:447) and BEFORE `print(swarm.wipe_plan_text(cands))`, annotate:

```python
    for c in cands:
        if (c.get("worktree_repo") and c.get("workdir")
                and os.path.isdir(c["workdir"])):
            c["worktree_action"] = ("keep-dirty"
                                    if _worktree_dirty(c["workdir"])
                                    else "remove")
```

And in the acting loop (:461-468), after `db.delete_session(conn, c["name"])`:

```python
        if c.get("worktree_action") == "remove":
            ok_rm, rm_err = _worktree_remove(c["worktree_repo"], c["workdir"],
                                             c["name"])
            print(f"  removed worktree {c['workdir']}" if ok_rm
                  else f"  worktree removal failed: {rm_err}")
```

- [ ] **Step 4: Run to verify pass**

Run: `./test/run.sh` -> all OK.

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/cli.py iterm/test_swarm.py iterm/test_cli.py
git commit -m "feat(wipe): remove clean relay-created worktrees, never dirty ones"
```

---

### Task 7: skills, CLI reference, README

**Files:**
- Modify: `skills/relay-coordinator/SKILL.md`, `skills/relay-worker/SKILL.md`, `skills/relay-cli-reference.md`, `README.md`

No code - documentation task; test is a careful read plus `grep` sanity checks below.

- [ ] **Step 1: `skills/relay-cli-reference.md`**

Update the `relay send` entry to:

```
relay send <name> "<body>" [--kind <k>]
relay send --all --project <p> "<body>" [--kind <k>]
    Queue a message for a named session (or every live session in the
    project except you, with --all). Delivered TYPED INTO their Claude
    prompt when they are idle and the relay TUI is running. Single line;
    newlines are flattened. --kind: info (default) | done | blocked |
    escalation | a custom lowercase token. 'escalation' also plays a
    sound + notification for the human IMMEDIATELY - use it only when a
    human decision is genuinely required. 'wake' is reserved.
```

Update the `relay spawn` entry, adding:

```
            [--worktree]
    ... --worktree (with --dir <repo>): create branch relay/<name> and a
    sibling git worktree <repo>-<name>, and spawn the worker THERE - use it
    whenever 2+ workers touch the same repo, so they cannot clobber each
    other's files.
```

- [ ] **Step 2: `skills/relay-coordinator/SKILL.md`**

In "## Orchestrating" step 2, extend the spawn guidance with:

```
   When 2+ workers will touch the SAME repo, add --worktree so each gets an
   isolated git worktree (branch relay/<name>, sibling dir <repo>-<name>):
   `relay spawn --name <worker> --project <project> --dir <repo-path> --worktree --arm wild "<short mission>"`
   Prefer this over pointing two workers at one working copy - parallel
   sessions editing the same files clobber each other. If you need a custom
   layout, create the worktree yourself (`git worktree add`) and pass --dir;
   relay never forces the layout.
```

Add a new section after "## Reacting (event-driven, not polling)":

```
## Integrating worktree branches

Workers on worktrees commit to relay/<name> and report the branch in their
done message. Merging is YOURS (or the human's), via normal git:
- on done: review the branch, then merge/rebase it into the target branch
  yourself, or escalate to the human if conflicts need judgment.
- do not mark the epic done until its branch is integrated or explicitly
  parked.
```

In "## Reacting", extend the message-kind guidance by replacing the first bullet with:

```
- Workers report via messages that arrive as `[relay <kind> from <name>]`
  turns (msg = plain info). Treat `done` as queue-advance, `blocked` and
  `escalation` as interrupts to handle now. React to those; between them,
  stay idle.
```

- [ ] **Step 3: `skills/relay-worker/SKILL.md`**

Replace "Working an assigned task" step 5 with:

```
5. When the work is done: commit it first - on a worktree you are on branch
   relay/<your-name>; commit everything there (an uncommitted worktree
   blocks cleanup and can be lost). Then `relay task update <epic-id>
   --state done` and
   `relay send <coordinator> "task #<id> done on branch relay/<your-name>: <one-line summary>" --kind done`.
   (Not on a worktree? Same rule, minus the branch name.)
```

In "## Never go silent", update the two send examples to carry kinds:
- blocked example gains `--kind blocked`
- the "need a decision" example gains `--kind escalation`, plus this sentence:

```
  `--kind escalation` plays a sound for the human immediately - use it when
  you need a HUMAN, not for routine coordinator questions (those are
  --kind blocked).
```

- [ ] **Step 4: `README.md`**

- In the CLI verb block (:383-398): update the `relay send` and `relay spawn` entries to match the reference text from Step 1 (condensed to the README's existing style).
- After the "### Delivery" section, add a short "### Message kinds" subsection: kinds list, `[relay done from bff]` prefix example, escalation ping behavior, `wake` reserved.
- In "### relay spawn" (:446-453): a sentence on `--worktree` (what it creates, when to use it).
- In "### Recovering abandoned work" wipe block (:499-524): a sentence that wipe also removes relay-created worktrees when clean, and always keeps dirty ones.
- In "## Swarm" intro example (:287-306): show one spawn with `--worktree`.

- [ ] **Step 5: Sanity checks + commit**

```bash
grep -c "worktree" skills/relay-coordinator/SKILL.md   # expect >= 2
grep -c "kind" skills/relay-worker/SKILL.md            # expect >= 2
grep -n "—" README.md skills/*.md skills/*/SKILL.md    # expect NO matches (em-dash ban)
git add skills/ README.md
git commit -m "docs: worktree spawn + message kinds in skills, reference, README"
```

---

### Task 8: full verification + spec status

**Files:**
- Modify: `docs/drafts/worktree-typed-messages-design.md` (status line)

- [ ] **Step 1: Full suite**

Run: `./test/run.sh`
Expected: every suite prints all `OK`, exit 0.

- [ ] **Step 2: Migration-on-update check (the auto-updater question)**

Run: `python3 -c "import sys; sys.path.insert(0, 'iterm'); import db, tempfile, os; p=os.path.join(tempfile.mkdtemp(),'x.db'); c=db.connect(p); print('schema v', c.execute('PRAGMA user_version').fetchone()[0])"`
Expected: `schema v 5`. This is the whole update story: `relay update` fast-forwards the checkout, and the next `db.connect()` (any CLI verb or TUI launch) applies pending migrations automatically.

- [ ] **Step 3: Hand-check note (live paths)**

The iTerm2-live halves (real spawn into a worktree tab, real escalation notification sound) follow the repo's existing convention: hand-checked, not CI. Print the two commands for the human to smoke-test later:

```
relay spawn --name wt-smoke --project smoke --dir <some-repo> --worktree --arm wild "await your task via relay inbox"
relay send <coordinator-name> "smoke escalation" --kind escalation   # from another registered session
```

- [ ] **Step 4: Mark spec implemented + commit**

In `docs/drafts/worktree-typed-messages-design.md`, change the `Status:` line to `implemented (see this plan's commits)`. Then:

```bash
git add docs/drafts/worktree-typed-messages-design.md
git commit -m "docs: mark worktree + typed-messages spec implemented"
```
