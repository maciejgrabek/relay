# Session Conversations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let two or more Claude sessions talk to each other and settle a
question without the operator carrying messages between tabs.

**Architecture:** No new processes. The SQLite DB stays the bus and the watcher
stays the only thing that types into a tab. This adds two columns on
`messages`, one `threads` table, a handful of CLI verbs, and one evaluation
step in the watcher's existing per-tick sweep. Agreement is derived from
messages rather than tracked separately.

**Tech Stack:** Python 3 stdlib only (sqlite3, argparse, asyncio). iTerm2
Python API in the watcher layer only. No test framework - each suite is a
plain script.

**Spec:** `docs/specs/2026-08-03-session-conversations-design.md`

**Status: COMPLETE (2026-08-03).** All 17 tasks implemented, `./test/run.sh`
green. Two corrections made during execution, both folded back into the spec:
name derivation cannot use the iTerm2 tab title (the CLI has no access to it),
and a fan-out post must carry ONE timestamp across its rows or the transcript
de-dupe never matches. Three items were added beyond the plan: `prune_threads`
(closed discussions must not outlive their transcripts), thread context in
`relay inbox` / `relay msgs`, and the coordinator skill's discussion section.

## Global Constraints

- **No pytest.** Each suite is `iterm/test_*.py` with a `check(msg, cond)`
  helper, a `run()` returning bool, and a `__main__` block doing
  `sys.exit(0 if run() else 1)`. `test/run.sh` auto-discovers `iterm/test_*.py`.
- **Run the suite with `./test/run.sh`.** A single suite runs with
  `python3 iterm/test_<name>.py`.
- **No em-dash characters (U+2014) anywhere** - source, comments, docs, commit
  messages. Use a plain ASCII hyphen.
- **No `Co-Authored-By` trailer** in commit messages.
- **`iterm/db.py`, `iterm/swarm.py`, `iterm/titles.py`, `iterm/timers.py` are
  pure stdlib** - no `iterm2` imports. Keep it that way; that is what makes
  them unit-testable.
- **New tables go in `_SCHEMA`** (a `CREATE TABLE IF NOT EXISTS`, a no-op on
  existing DBs). **New columns on existing tables go in `_MIGRATIONS`.** Never
  put a migration-added column in an index inside `_SCHEMA`.
- **Audit before act.** Any new code path that types into a tab must call
  `audit.record(...)` and bail if it returns False, before sending.
- **Reserved names** are `relay` and `human` (`db.RESERVED_NAMES`). Nothing may
  register as one and nothing may be injected into one.
- Current schema state: `_CURRENT_VERSION = 9`, `_MIGRATIONS` has keys 1..8.
  This plan adds key `9` and bumps `_CURRENT_VERSION` to `10`.

## File Structure

**Modified:**
- `iterm/db.py` - migration 9, `threads` table in `_SCHEMA`, `reply_to` /
  `thread_id` on `queue_message`, thread CRUD, `last_batch`, `live_names`.
- `iterm/swarm.py` - pure helpers: `derive_name`, `batch_delivery_text`,
  `thread_pointer_text`, `agreement_state`, `round_counts`.
- `iterm/cli.py` - `_ensure_me`, `who`, `join` (optional name), `reply`,
  `discuss`, `say`, `agree`, `thread`, `ask`.
- `iterm/watcher.py` - `_deliver` batching, `_close_threads` sweep step.
- `iterm/protocol.py` - `DISCUSS_PROTOCOL`, added to `TOPICS`.
- `iterm/app.py` - DISCUSSIONS pane in the swarm view.
- `bin/relay` - verb dispatch list and `-h` block.
- `skills/relay-worker/SKILL.md`, `skills/relay-cli-reference.md`, `README.md`.

**Test files (all existing, extended):** `iterm/test_db.py`,
`iterm/test_swarm.py`, `iterm/test_cli.py`, `iterm/test_watcher.py`.

**Deliberately unchanged:** `gates.py`, `danger.sh`, `audit.py`, `timers.py`,
`statusbar.py`, `widget.py`. This feature adds no new approval or arming
behavior.

---

# PHASE 1 - talk at all, with no setup

Ships on its own. Removes registration ceremony and the reply ceremony, and
lays the identity + envelope groundwork phase 2 depends on.

---

### Task 1: Schema - `reply_to` and `thread_id` on messages

**Files:**
- Modify: `iterm/db.py:152` (`_CURRENT_VERSION`), `iterm/db.py:153-206`
  (`_MIGRATIONS`), `iterm/db.py:379-390` (`queue_message`)
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces: `db.queue_message(conn, from_name, to_name, body, project="",
  now=None, kind="info", reply_to=None, thread_id=None) -> int`

- [ ] **Step 1: Write the failing test**

Add inside `run()` in `iterm/test_db.py`, after the existing schema-versioning
checks. Note the existing first check asserts version 9 - update it to 10 in
the same edit.

```python
    # --- migration 9: messages.reply_to / thread_id -------------------------
    mid = db.queue_message(conn, "a", "b", "hello")
    rid = db.queue_message(conn, "b", "a", "hi back", reply_to=mid)
    row = conn.execute("SELECT * FROM messages WHERE id=?", (rid,)).fetchone()
    ok &= check("queue_message records reply_to", row["reply_to"] == mid)
    ok &= check("thread_id defaults NULL", row["thread_id"] is None)
    tmsg = db.queue_message(conn, "a", "b", "in thread", thread_id=7)
    trow = conn.execute("SELECT * FROM messages WHERE id=?",
                        (tmsg,)).fetchone()
    ok &= check("queue_message records thread_id", trow["thread_id"] == 7)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_db.py`
Expected: FAIL - `queue_message() got an unexpected keyword argument
'reply_to'`, and the version check fails asserting 9 != 10.

- [ ] **Step 3: Write minimal implementation**

In `iterm/db.py`, bump the version and add the migration:

```python
_CURRENT_VERSION = 10
```

Add to `_MIGRATIONS`, after key `8`:

```python
    # v10: session conversations. `reply_to` correlates an answer with the
    # message it answers (so `relay ask` cannot mistake unrelated traffic for
    # its reply); `thread_id` marks a message as belonging to a discussion.
    # Both NULL on ordinary `relay send` traffic, so every existing message
    # path is untouched.
    9: ("ALTER TABLE messages ADD COLUMN reply_to INTEGER",
        "ALTER TABLE messages ADD COLUMN thread_id INTEGER"),
```

Replace `queue_message`:

```python
def queue_message(conn, from_name: str, to_name: str, body: str,
                  project: str = "", now: Optional[float] = None,
                  kind: str = "info", reply_to: Optional[int] = None,
                  thread_id: Optional[int] = None) -> int:
    cur = conn.execute(
        """INSERT INTO messages(project, from_name, to_name, body, created_at,
                                kind, reply_to, thread_id)
           VALUES(?,?,?,?,?,?,?,?)""",
        (project, from_name, to_name, body, _now(now), kind or "info",
         reply_to, thread_id))
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED. The migration test that walks v1 forward must
still land on the current version - if it asserts a literal, update it to 10.

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): messages.reply_to and thread_id, migration 10"
```

---

### Task 2: `swarm.derive_name` - a name without being told one

**Files:**
- Modify: `iterm/swarm.py`
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Produces: `swarm.derive_name(cwd: str, taken) -> str`

Derivation uses the working-directory basename only. The iTerm2 tab title is
NOT available to the CLI (iTerm2 exports only `$ITERM_SESSION_ID`; reading a
title needs the iTerm2 API, i.e. the TUI process). The spec's mention of the
tab title is superseded by this task.

- [ ] **Step 1: Write the failing test**

Add inside `run()` in `iterm/test_swarm.py`:

```python
    # --- derive_name --------------------------------------------------------
    ok &= check("derive_name uses the cwd basename",
                swarm.derive_name("/Users/x/Work/relay", set()) == "relay")
    ok &= check("derive_name slugifies",
                swarm.derive_name("/Users/x/My Big_Repo!", set())
                == "my-big-repo")
    ok &= check("derive_name dedupes with -2",
                swarm.derive_name("/Users/x/relay", {"relay"}) == "relay-2")
    ok &= check("derive_name dedupes past -2",
                swarm.derive_name("/Users/x/relay", {"relay", "relay-2"})
                == "relay-3")
    ok &= check("derive_name never yields a reserved name",
                swarm.derive_name("/tmp/human", set()) == "human-2")
    ok &= check("derive_name falls back when the basename is empty",
                swarm.derive_name("/", set()) == "session")
    ok &= check("derive_name truncates long basenames",
                len(swarm.derive_name("/x/" + "a" * 80, set())) <= 24)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL - `module 'swarm' has no attribute 'derive_name'`

- [ ] **Step 3: Write minimal implementation**

Add to `iterm/swarm.py`. It needs `import os` and `import re` at the top if not
already present, and `RESERVED_NAMES` - import it from `db` is NOT allowed
(swarm.py must stay import-light); redeclare the tuple locally as
`_RESERVED = ("relay", "human")` with a comment pointing at `db.RESERVED_NAMES`
as the source of truth.

```python
# Mirrors db.RESERVED_NAMES. Duplicated rather than imported so swarm.py stays
# dependency-free (it is unit-tested standalone). db.register enforces the real
# rule; this only stops us ever PROPOSING a name that would be refused.
_RESERVED = ("relay", "human")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_MAX = 24


def derive_name(cwd: str, taken) -> str:
    """A session name for a session nobody named, from its working directory.

    The operator's mental model is "the tab in <repo>", so the basename is the
    least surprising handle. Collisions are common (three sessions in one
    repo), so dedupe with a numeric suffix rather than something opaque - a
    human has to read these in `relay who`.
    """
    base = _SLUG_RE.sub("-", os.path.basename(str(cwd or "").rstrip("/"))
                        .lower()).strip("-")
    base = base[:_NAME_MAX].rstrip("-") or "session"
    taken = set(taken or ())
    if base not in taken and base not in _RESERVED:
        return base
    n = 2
    while f"{base}-{n}" in taken or f"{base}-{n}" in _RESERVED:
        n += 1
    return f"{base}-{n}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 iterm/test_swarm.py`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): derive_name - a session name from its workdir"
```

---

### Task 3: `_ensure_me` - auto-registration

**Files:**
- Modify: `iterm/cli.py:62-67` (`_require_me`), `iterm/db.py` (add
  `live_names`)
- Test: `iterm/test_cli.py`, `iterm/test_db.py`

**Interfaces:**
- Consumes: `swarm.derive_name` (Task 2)
- Produces: `cli._ensure_me(conn) -> (row, int)`, `db.live_names(conn) -> set`

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_db.py` inside `run()`:

```python
    ok &= check("live_names excludes closed sessions",
                "b" not in db.live_names(conn)
                if db.get_session(conn, "b") is None else True)
```

Add to `iterm/test_cli.py` inside `run()`. Use a fresh iTerm id so this session
is genuinely unregistered:

```python
    # --- auto-registration --------------------------------------------------
    code, out, err = run_cli("status", "auto-registered and working",
                             iterm_id="w0t9p0:AUTO-9999")
    ok &= check("status auto-registers an unknown session", code == 0)
    c = db.connect()
    auto = c.execute("SELECT * FROM sessions WHERE iterm_session_id=?",
                     ("AUTO-9999",)).fetchone()
    c.close()
    ok &= check("auto-registered session exists", auto is not None)
    ok &= check("auto-registered as worker",
                auto is not None and auto["role"] == "worker")
    ok &= check("auto-register announces the derived name",
                auto is not None and auto["name"] in out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - `status` returns 1 with "this session is not registered".

- [ ] **Step 3: Write minimal implementation**

Add to `iterm/db.py` in the sessions section:

```python
def live_names(conn) -> set:
    """Every name currently bound to a non-closed session."""
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sessions WHERE closed_at = 0").fetchall()}
```

Replace `_require_me` in `iterm/cli.py` and keep the old name as an alias so
untouched call sites keep working:

```python
def _ensure_me(conn):
    """This session's row, auto-registering it if it has none.

    Registration used to be an explicit act. It still is, in the sense that a
    session only becomes addressable by RUNNING a relay verb itself - a tab
    that never touches relay stays untouchable by the watcher's delivery leg.
    What changes is that the act no longer has to be a separate command with a
    name the operator invented, which was the whole barrier to "just talk to
    the other session".
    """
    me = whoami(conn)
    if me is not None:
        return me, 0
    sid = my_iterm_id()
    if not sid:
        return None, _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    name = swarm.derive_name(os.getcwd(), db.live_names(conn))
    db.register(conn, name, sid, "worker", _default_project(conn))
    db.set_session_context(conn, name, os.getcwd(), "")
    print(f"relay: registered this session as '{name}' "
          f"(rename with: relay join <name>)")
    return db.get_session(conn, name), 0


# Historical name. Auto-registration made "require" the wrong verb, but the
# call sites read fine either way.
_require_me = _ensure_me
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED. Existing tests asserting that an unregistered
session is REFUSED will now fail - those assertions encoded the old rule and
must be updated to assert auto-registration instead. Do not weaken any test
that asserts a RESERVED name is refused.

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/db.py iterm/test_cli.py iterm/test_db.py
git commit -m "feat(cli): sessions auto-register on first use"
```

---

### Task 4: `relay who`

**Files:**
- Modify: `iterm/cli.py` (new `cmd_who`, parser entry), `bin/relay`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Produces: `cli.cmd_who(args) -> int`

- [ ] **Step 1: Write the failing test**

```python
    # --- who ----------------------------------------------------------------
    run_cli("join", "who-a", iterm_id="w0t1p0:WHO-A")
    run_cli("join", "who-b", iterm_id="w0t1p0:WHO-B")
    code, out, err = run_cli("who", iterm_id="w0t1p0:WHO-A")
    ok &= check("who exits 0", code == 0)
    ok &= check("who lists peers", "who-b" in out)
    ok &= check("who marks me", "(you)" in out)
    ok &= check("who teaches the talk verbs", "relay discuss" in out)
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM sessions WHERE iterm_session_id=?",
                  ("WHO-C",)).fetchone()[0]
    c.close()
    code, out, err = run_cli("who", iterm_id="w0t1p0:WHO-C")
    c = db.connect()
    n2 = c.execute("SELECT COUNT(*) FROM sessions WHERE iterm_session_id=?",
                   ("WHO-C",)).fetchone()[0]
    c.close()
    ok &= check("who does not auto-register the caller", n == n2 == 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse errors with "invalid choice: 'who'".

- [ ] **Step 3: Write minimal implementation**

Add to `iterm/cli.py`:

```python
def cmd_who(args) -> int:
    """Who can I talk to? Read-only: reading the roster is not joining it."""
    conn = db.connect()
    me = whoami(conn)
    rows = [s for s in db.list_sessions(conn, args.project)
            if not s["closed_at"]]
    if not rows:
        print("nobody is registered yet")
        return 0
    print(f"{'NAME':<18} {'ROLE':<12} {'SEEN':<10} STATUS")
    for s in rows:
        mine = "  (you)" if me is not None and s["name"] == me["name"] else ""
        print(f"{s['name']:<18} {s['role']:<12} "
              f"{_ago(s['last_seen']):<10} {s['status_text'] or '-'}{mine}")
    print()
    print("talk to one:  relay send <name> \"<body>\"")
    print("settle something with several:  "
          "relay discuss <name> <name> \"<topic>\"")
    return 0
```

Parser entry, next to the `msgs` parser:

```python
    wh = sub.add_parser("who", help="who else is here (read-only)")
    wh.add_argument("--project", default=None)
    wh.set_defaults(fn=cmd_who)
```

In `bin/relay`, add `who` to the dispatch alternation on line 30 and to the
verb list in the `-h` comment block.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 iterm/test_cli.py` then `./test/run.sh`
Expected: ALL PASS / ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py bin/relay
git commit -m "feat(cli): relay who - the roster, without joining it"
```

---

### Task 5: `relay join` with no name

**Files:**
- Modify: `iterm/cli.py:150-206` (`cmd_join`), parser entry at `iterm/cli.py:1364`
- Test: `iterm/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
    # --- join with no name --------------------------------------------------
    code, out, err = run_cli("join", iterm_id="w0t1p0:BARE-1")
    ok &= check("bare join exits 0", code == 0)
    c = db.connect()
    row = c.execute("SELECT * FROM sessions WHERE iterm_session_id=?",
                    ("BARE-1",)).fetchone()
    c.close()
    ok &= check("bare join registers a derived name", row is not None)
    ok &= check("bare join prints the roster", "SWARM ROSTER" in out)
    ok &= check("bare join prints the protocol", "relay send" in out)
    code, out, err = run_cli("join", "renamed-1", iterm_id="w0t1p0:BARE-1")
    ok &= check("join <name> renames in place", code == 0)
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM sessions WHERE iterm_session_id=?",
                  ("BARE-1",)).fetchone()[0]
    c.close()
    ok &= check("rename leaves exactly one row for the tab", n == 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse errors, `join` requires the `name` positional.

- [ ] **Step 3: Write minimal implementation**

Parser: make the positional optional.

```python
    j.add_argument("name", nargs="?", default=None)
```

In `cmd_join`, replace the name-resolution preamble (the `args.name.strip()`
block) with:

```python
    existing_me = whoami(conn) if (conn := db.connect()) else None
    if args.name is None:
        # Already registered? Bare `join` is then a re-orientation, not a
        # rename: keep the name this session already answers to.
        name = (existing_me["name"] if existing_me is not None
                else swarm.derive_name(os.getcwd(), db.live_names(conn)))
    else:
        name = args.name.strip()
        if not name:
            return _err("name cannot be empty")
        if name in db.RESERVED_NAMES:
            return _err(f"'{name}' is reserved - 'relay' is the sender of "
                        f"system wake-ups and 'human' is the operator's "
                        f"escalation mailbox; pick another name")
        if existing_me is not None and existing_me["name"] != name:
            db.rename_session(conn, existing_me["name"], name)
```

Note: the walrus above is only to keep the diff small - if `conn` is already
assigned earlier in the function, use the existing variable and drop it.

Add `rename_session` to `iterm/db.py`:

```python
def rename_session(conn, old: str, new: str) -> None:
    """Rebind a session to a new name, carrying its mail and tasks with it.
    Auto-registered names are meant to be replaced, so a rename must not
    orphan the session's history."""
    if new in RESERVED_NAMES:
        raise ValueError(f"'{new}' is reserved")
    with conn:
        conn.execute("UPDATE sessions SET name=? WHERE name=?", (new, old))
        conn.execute("UPDATE messages SET from_name=? WHERE from_name=?",
                     (new, old))
        conn.execute("UPDATE messages SET to_name=? WHERE to_name=?",
                     (new, old))
        conn.execute("UPDATE tasks SET owner=? WHERE owner=?", (new, old))
        conn.execute("UPDATE tasks SET created_by=? WHERE created_by=?",
                     (new, old))
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/db.py iterm/test_cli.py
git commit -m "feat(cli): relay join with no name - the zero-setup entry point"
```

---

### Task 6: Batched delivery text

**Files:**
- Modify: `iterm/swarm.py:57-68` (`delivery_text`)
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Produces: `swarm.batch_delivery_text(msgs) -> str`

- [ ] **Step 1: Write the failing test**

```python
    # --- batch_delivery_text ------------------------------------------------
    one = [{"id": 5, "from_name": "a", "body": "just this", "kind": "info"}]
    t = swarm.batch_delivery_text(one)
    ok &= check("single message keeps the inline body", "just this" in t)
    ok &= check("single message names its id", "5" in t)
    ok &= check("single message teaches reply", "relay reply" in t)
    ok &= check("delivery text is one line", "\n" not in t)

    many = [{"id": i, "from_name": f"s{i}", "body": f"body{i}",
             "kind": "info"} for i in range(1, 4)]
    tm = swarm.batch_delivery_text(many)
    ok &= check("batch is one line", "\n" not in tm)
    ok &= check("batch counts the messages", "3" in tm)
    ok &= check("batch points at inbox", "relay inbox" in tm)

    huge = [{"id": 1, "from_name": "a", "body": "x" * 5000, "kind": "info"}]
    ok &= check("delivery text is bounded",
                len(swarm.batch_delivery_text(huge)) <= 700)
    ok &= check("control characters are stripped",
                "\x1b" not in swarm.batch_delivery_text(
                    [{"id": 1, "from_name": "a", "body": "a\x1b[Bb",
                      "kind": "info"}]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL - `module 'swarm' has no attribute 'batch_delivery_text'`

- [ ] **Step 3: Write minimal implementation**

Keep `delivery_text` as-is (other call sites and tests use it) and add:

```python
_DELIVERY_MAX = 700


def batch_delivery_text(msgs) -> str:
    """The literal text typed into a session for its whole queued batch.

    One injected turn per batch, not per message: every delivery costs the
    recipient a full Claude turn, so three queued messages used to cost three
    turns to convey what one turn can. Still ONE line and one Enter - the
    bracketed-paste constraint has not changed - so a batch degrades to a
    count plus a pointer rather than trying to inline everything.
    """
    msgs = list(msgs or ())
    if not msgs:
        return ""
    if len(msgs) == 1:
        m = msgs[0]
        base = delivery_text(_get(m, "from_name"), _get(m, "body"),
                             kind_of(m))
        mid = _get(m, "id")
        return _clip(f"{base}  (reply: relay reply {mid} \"<your answer>\")")
    senders = []
    for m in msgs:
        s = _get(m, "from_name")
        if s not in senders:
            senders.append(s)
    who = ", ".join(senders[:4]) + ("..." if len(senders) > 4 else "")
    return _clip(f"[relay {len(msgs)} messages from {who}] "
                 f"read them: relay inbox")


def _clip(s: str) -> str:
    flat = " ".join(str(s).splitlines())
    flat = "".join(c for c in flat if c.isprintable() or c == " ")
    return flat[:_DELIVERY_MAX]
```

If `_get(m, key)` does not already exist in `swarm.py`, add it next to
`kind_of` (which uses the same sqlite-Row-or-dict tolerance):

```python
def _get(m, key, default=""):
    try:
        return m[key] if key in m.keys() else default
    except Exception:
        return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 iterm/test_swarm.py`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): batch_delivery_text - one injected turn per batch"
```

---

### Task 7: Watcher delivers the batch

**Files:**
- Modify: `iterm/watcher.py:756-818` (`_deliver`)
- Test: `iterm/test_watcher.py`

- [ ] **Step 1: Write the failing test**

Follow the existing `_deliver` test in `iterm/test_watcher.py` for the fake
session/reactor scaffolding. Add:

```python
    # --- batched delivery ---------------------------------------------------
    # three queued messages must cost ONE injected turn, all marked delivered
    # with a single shared timestamp (the batch identity `relay reply` needs).
```

Queue three messages to a registered, idle session, run one tick, then assert:
one `async_send_text` body call (plus its standalone `"\r"`), all three rows
have a non-NULL `delivered_at`, and `len({row["delivered_at"] for row in rows})
== 1`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL - only one of the three is marked delivered per tick.

- [ ] **Step 3: Write minimal implementation**

In `_deliver`, replace `m = msgs[0]` and everything after it. Keep the
own-sid, paused, registry, reserved-name and idle guards above it untouched.

```python
        text = swarm.batch_delivery_text(msgs)
        ids = [m["id"] for m in msgs]
        if self.dry_run:
            for m in msgs:
                if m["id"] not in self._dryrun_delivered:
                    self._dryrun_delivered.add(m["id"])
                    audit.record("would-deliver", info.title, text[:500],
                                 f"msg {m['id']} to {reg['name']}")
            self._note(f"DRY-RUN would deliver -> {reg['name']}: "
                       f"{len(msgs)} msg(s)")
            return
        # LOG BEFORE ACT (same contract as approvals) - one record per
        # message, so the audit trail still accounts for every message even
        # though they share one injected turn.
        for m in msgs:
            if not audit.record("delivered", info.title, text[:500],
                                f"msg {m['id']} from {m['from_name']} "
                                f"to {reg['name']}"):
                now = time.time()
                if now - info._last_notify_ts >= self.notify_cooldown:
                    info._last_notify_ts = now
                    self._note(f"AUDIT-FAIL: not delivering msg {m['id']}")
                    notify_mac("Relay - swarm", "audit log write failed - "
                               "NOT delivering message", self.alert_sound)
                return
        await info._iterm_session.async_send_text(text)
        await asyncio.sleep(0.3)
        await info._iterm_session.async_send_text("\r")
        # ONE timestamp for the whole batch: `relay reply` with no id refuses
        # when the last delivery held more than one message, and a shared
        # delivered_at is what makes "the last batch" answerable.
        stamp = time.time()
        for mid in ids:
            swarmdb.mark_delivered(self._swarm_conn(), mid, now=stamp)
        self._note(f"DELIVER -> {reg['name']}: {len(ids)} msg(s)")
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "feat(watcher): deliver a session's whole queue in one turn"
```

---

### Task 8: `relay reply`

**Files:**
- Modify: `iterm/db.py` (add `last_batch`), `iterm/cli.py` (add `cmd_reply`,
  parser entry), `bin/relay`
- Test: `iterm/test_db.py`, `iterm/test_cli.py`

**Interfaces:**
- Produces: `db.last_batch(conn, to_name) -> List[Row]`, `cli.cmd_reply`

- [ ] **Step 1: Write the failing test**

In `iterm/test_cli.py`:

```python
    # --- reply --------------------------------------------------------------
    run_cli("join", "rep-a", iterm_id="w0t1p0:REP-A")
    run_cli("join", "rep-b", iterm_id="w0t1p0:REP-B")
    run_cli("send", "rep-b", "question one", iterm_id="w0t1p0:REP-A")
    c = db.connect()
    qid = c.execute("SELECT id FROM messages WHERE to_name='rep-b' "
                    "ORDER BY id DESC LIMIT 1").fetchone()[0]
    c.execute("UPDATE messages SET delivered_at=1000.0 WHERE id=?", (qid,))
    c.commit(); c.close()
    code, out, err = run_cli("reply", "my answer", iterm_id="w0t1p0:REP-B")
    ok &= check("bare reply exits 0", code == 0)
    row = _one_message("rep-a")
    ok &= check("bare reply targets the sender", row["from_name"] == "rep-b")
    ok &= check("bare reply correlates", row["reply_to"] == qid)

    # two messages sharing one delivered_at = one batch: bare reply refuses
    c = db.connect()
    c.execute("INSERT INTO messages(project,from_name,to_name,body,"
              "created_at,kind,delivered_at) VALUES "
              "('','rep-a','rep-b','m1',1,'info',2000.0)")
    c.execute("INSERT INTO messages(project,from_name,to_name,body,"
              "created_at,kind,delivered_at) VALUES "
              "('','rep-a','rep-b','m2',2,'info',2000.0)")
    c.commit(); c.close()
    code, out, err = run_cli("reply", "ambiguous", iterm_id="w0t1p0:REP-B")
    ok &= check("bare reply refuses after a batch", code != 0)
    ok &= check("refusal lists the ids", "#" in err)
    code, out, err = run_cli("reply", "1", "explicit",
                             iterm_id="w0t1p0:REP-B")
    ok &= check("reply <id> works after a batch", code == 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - "invalid choice: 'reply'"

- [ ] **Step 3: Write minimal implementation**

`iterm/db.py`:

```python
def last_batch(conn, to_name: str) -> List[sqlite3.Row]:
    """The messages delivered to `to_name` in its most recent delivery.

    The watcher stamps one delivered_at across a whole batch, so a shared
    timestamp IS the batch. Used by `relay reply` with no id: replying to "the
    last message" is only unambiguous when the last delivery held exactly one.
    """
    row = conn.execute(
        "SELECT MAX(delivered_at) AS t FROM messages "
        "WHERE to_name=? AND delivered_at IS NOT NULL", (to_name,)).fetchone()
    if row is None or row["t"] is None:
        return []
    return conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND delivered_at=? "
        "ORDER BY id", (to_name, row["t"])).fetchall()
```

`iterm/cli.py`:

```python
def cmd_reply(args) -> int:
    """Answer whoever wrote to you, without having to know their name.

    This is the ceremony `relay send <name>` used to demand: a session had to
    parse the sender out of its injected turn and retype it. Getting that
    wrong is silent - the message goes to a real session that never asked."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    if args.target is None:
        return _err('usage: relay reply "<body>"  or  '
                    'relay reply <msg-id> "<body>"')
    if args.body is None:
        mid, body = None, args.target
    else:
        if not str(args.target).isdigit():
            return _err(f"expected a message id, got {args.target!r} - "
                        f'usage: relay reply <msg-id> "<body>"')
        mid, body = int(args.target), args.body
    if mid is None:
        batch = db.last_batch(conn, me["name"])
        if not batch:
            return _err("nothing to reply to - you have received no messages. "
                        'Use: relay send <name> "<body>" (relay who lists '
                        'names)')
        if len(batch) > 1:
            ids = ", ".join(f"#{m['id']} from {m['from_name']}"
                            for m in batch)
            return _err(f"your last delivery held {len(batch)} messages "
                        f"({ids}) - say which one: "
                        f'relay reply <msg-id> "<body>"')
        mid = batch[0]["id"]
    src = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if src is None:
        return _err(f"no message #{mid}")
    if src["to_name"] != me["name"]:
        return _err(f"message #{mid} was not addressed to you")
    target = src["from_name"]
    if target in db.RESERVED_NAMES:
        return _err(f"#{mid} came from '{target}', which is not a session you "
                    f"can answer. If you need a decision, use: "
                    f'relay send --human "<the question>"')
    if db.get_session(conn, target) is None:
        return _err(f"'{target}' is no longer registered - "
                    f'relay who lists who is here')
    db.queue_message(conn, me["name"], target, body, me["project"],
                     kind=args.kind or "info", reply_to=mid)
    print(f"replied to {target} (re: #{mid})")
    return 0
```

Parser, after the `send` parser:

```python
    rp_ = sub.add_parser("reply", help="answer the last message you received")
    rp_.add_argument("target", nargs="?", default=None,
                     help="message id, or the body when replying to the last")
    rp_.add_argument("body", nargs="?", default=None)
    rp_.add_argument("--kind", default="info")
    rp_.set_defaults(fn=cmd_reply)
```

Add `reply` to `bin/relay`'s dispatch alternation and `-h` block.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/db.py iterm/test_cli.py iterm/test_db.py bin/relay
git commit -m "feat(cli): relay reply - answer without retyping a name"
```

---

# PHASE 2 - discussions

Delivers the motivating scenario. Depends on Tasks 1-8.

---

### Task 9: `threads` table and thread CRUD

**Files:**
- Modify: `iterm/db.py` (`_SCHEMA`, new thread functions)
- Test: `iterm/test_db.py`

**Interfaces:**
- Produces:
  - `db.THREAD_STATES = ("open", "agreed", "unresolved")`
  - `db.create_thread(conn, topic, opener, participants, project="", rounds_cap=3, now=None) -> int`
  - `db.get_thread(conn, tid) -> Optional[Row]`
  - `db.list_threads(conn, project=None, state=None) -> List[Row]`
  - `db.thread_messages(conn, tid) -> List[Row]`
  - `db.close_thread(conn, tid, state, outcome, now=None) -> bool`
  - `db.participants_of(row) -> List[str]`

- [ ] **Step 1: Write the failing test**

```python
    # --- threads ------------------------------------------------------------
    tid = db.create_thread(conn, "one DB or many?", "a", ["a", "b", "c"],
                           project="p", rounds_cap=3)
    th = db.get_thread(conn, tid)
    ok &= check("thread starts open", th["state"] == "open")
    ok &= check("thread records the opener", th["opener"] == "a")
    ok &= check("participants round-trip",
                db.participants_of(th) == ["a", "b", "c"])
    ok &= check("rounds_cap stored", th["rounds_cap"] == 3)
    db.queue_message(conn, "a", "b", "my view", "p", kind="say",
                     thread_id=tid)
    ok &= check("thread_messages finds the post",
                len(db.thread_messages(conn, tid)) == 1)
    ok &= check("open threads listed",
                tid in [t["id"] for t in db.list_threads(conn, state="open")])
    ok &= check("close_thread sets state and outcome",
                db.close_thread(conn, tid, "agreed", "one per service"))
    th = db.get_thread(conn, tid)
    ok &= check("closed thread is agreed", th["state"] == "agreed")
    ok &= check("outcome stored", th["outcome"] == "one per service")
    ok &= check("closed_at set", th["closed_at"] > 0)
    ok &= check("closing twice is refused",
                not db.close_thread(conn, tid, "unresolved", "x"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_db.py`
Expected: FAIL - `module 'db' has no attribute 'create_thread'`

- [ ] **Step 3: Write minimal implementation**

Append to `_SCHEMA` in `iterm/db.py` (new table, no migration needed - the
established idiom):

```sql
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  opener TEXT NOT NULL,
  participants TEXT NOT NULL,
  rounds_cap INTEGER NOT NULL DEFAULT 3,
  state TEXT NOT NULL DEFAULT 'open',
  outcome TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  closed_at REAL NOT NULL DEFAULT 0
);
```

Add near `MESSAGE_KINDS`:

```python
THREAD_STATES = ("open", "agreed", "unresolved")
```

and extend `MESSAGE_KINDS` to
`("info", "done", "blocked", "escalation", "wake", "say", "agree", "ask")`.

Add a threads section to `iterm/db.py`:

```python
# --- threads -------------------------------------------------------------------

def participants_of(row) -> List[str]:
    return [p for p in str(row["participants"]).split(",") if p]


def create_thread(conn, topic: str, opener: str, participants,
                  project: str = "", rounds_cap: int = 3,
                  now: Optional[float] = None) -> int:
    names = [opener] + [p for p in participants if p != opener]
    cur = conn.execute(
        """INSERT INTO threads(project, topic, opener, participants,
                               rounds_cap, created_at)
           VALUES(?,?,?,?,?,?)""",
        (project, topic, opener, ",".join(names), int(rounds_cap),
         _now(now)))
    conn.commit()
    return cur.lastrowid


def get_thread(conn, tid: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()


def list_threads(conn, project: Optional[str] = None,
                 state: Optional[str] = None) -> List[sqlite3.Row]:
    where, args = [], []
    if project is not None:
        where.append("project=?"); args.append(project)
    if state is not None:
        where.append("state=?"); args.append(state)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT * FROM threads {w} ORDER BY id", tuple(args)).fetchall()


def thread_messages(conn, tid: int) -> List[sqlite3.Row]:
    """Every post in the thread, oldest first. Deduped by id: a post to three
    participants is three message rows carrying the same body, but the
    transcript must show it once."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at, id",
        (tid,)).fetchall()
    seen, out = set(), []
    for r in rows:
        k = (r["from_name"], r["body"], r["kind"], round(r["created_at"], 3))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def close_thread(conn, tid: int, state: str, outcome: str,
                 now: Optional[float] = None) -> bool:
    """Close an OPEN thread. Guarded on state so two watcher ticks racing
    cannot double-close and double-ping the operator."""
    if state not in THREAD_STATES:
        raise ValueError(f"state must be one of {THREAD_STATES}, got {state!r}")
    cur = conn.execute(
        "UPDATE threads SET state=?, outcome=?, closed_at=? "
        "WHERE id=? AND state='open'",
        (state, outcome, _now(now), tid))
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED. The `wipe`/`prune` tests may need the new table
added to their expectations - if `wipe` deletes per-project rows, add
`threads` to it (see Task 14).

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): threads table - a discussion with participants and a cap"
```

---

### Task 10: Agreement and round accounting (pure)

**Files:**
- Modify: `iterm/swarm.py`
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Produces:
  - `swarm.round_counts(msgs) -> dict[str, int]`
  - `swarm.positions(msgs) -> dict[str, str]`
  - `swarm.thread_verdict(participants, msgs, rounds_cap) -> (state, outcome)`
    where `state` is `"open" | "agreed" | "unresolved"`

- [ ] **Step 1: Write the failing test**

```python
    # --- thread verdicts ----------------------------------------------------
    def _m(frm, kind, body, t):
        return {"from_name": frm, "kind": kind, "body": body,
                "created_at": t, "id": int(t)}

    parts = ["a", "b"]
    posts = [_m("a", "say", "I think X", 1)]
    ok &= check("one post is not agreement",
                swarm.thread_verdict(parts, posts, 3)[0] == "open")
    ok &= check("round_counts counts says",
                swarm.round_counts(posts) == {"a": 1})
    ok &= check("agree does not consume a round",
                swarm.round_counts(posts + [_m("a", "agree", "X", 2)])
                == {"a": 1})

    both = posts + [_m("a", "agree", "X", 2), _m("b", "agree", "X too", 3)]
    st, outcome = swarm.thread_verdict(parts, both, 3)
    ok &= check("unanimous agree closes agreed", st == "agreed")
    ok &= check("outcome carries both positions",
                "X" in outcome and "X too" in outcome)

    retracted = both + [_m("b", "say", "actually, wait", 4)]
    ok &= check("a say after agree retracts it",
                swarm.thread_verdict(parts, retracted, 3)[0] == "open")

    capped = [_m(n, "say", f"post {i}", i) for i in range(1, 4)
              for n in ("a", "b")]
    st2, out2 = swarm.thread_verdict(parts, capped, 3)
    ok &= check("cap spent with no unanimity is unresolved",
                st2 == "unresolved")
    ok &= check("unresolved records last positions", "post 3" in out2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL - `module 'swarm' has no attribute 'round_counts'`

- [ ] **Step 3: Write minimal implementation**

```python
def round_counts(msgs) -> dict:
    """`say` posts per participant. `agree` deliberately does not count:
    settling must never be rationed, or a session at its cap could be unable
    to record the position it just reached."""
    out = {}
    for m in msgs:
        if kind_of(m) == "say":
            n = _get(m, "from_name")
            out[n] = out.get(n, 0) + 1
    return out


def positions(msgs) -> dict:
    """Each participant's live position: its most recent `agree`, unless it
    has posted a `say` since. A session that is still talking is not settled -
    that is the whole retraction rule, and it falls out of ordering."""
    out = {}
    for m in sorted(msgs, key=lambda x: (_get(x, "created_at", 0),
                                         _get(x, "id", 0))):
        k, who = kind_of(m), _get(m, "from_name")
        if k == "agree":
            out[who] = _get(m, "body")
        elif k == "say":
            out.pop(who, None)
    return out


def last_said(msgs) -> dict:
    """Each participant's most recent utterance of any kind - what an
    unresolved thread reports instead of an agreement."""
    out = {}
    for m in sorted(msgs, key=lambda x: (_get(x, "created_at", 0),
                                         _get(x, "id", 0))):
        if kind_of(m) in ("say", "agree"):
            out[_get(m, "from_name")] = _get(m, "body")
    return out


def thread_verdict(participants, msgs, rounds_cap: int):
    """(state, outcome) for a thread. 'open' means keep going.

    Unresolved is a NORMAL outcome, not a failure: two sessions that cannot
    converge is information the operator wants, and pretending otherwise would
    mean either looping forever or fabricating an agreement.
    """
    parts = list(participants)
    pos = positions(msgs)
    if parts and all(p in pos for p in parts):
        return "agreed", " | ".join(f"{p}: {pos[p]}" for p in parts)
    counts = round_counts(msgs)
    if parts and all(counts.get(p, 0) >= rounds_cap for p in parts):
        said = last_said(msgs)
        return "unresolved", " | ".join(
            f"{p}: {said.get(p, '(never posted)')}" for p in parts)
    return "open", ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 iterm/test_swarm.py`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): derive agreement, rounds and thread verdicts"
```

---

### Task 11: `relay discuss` / `say` / `agree` / `thread`

**Files:**
- Modify: `iterm/cli.py`, `bin/relay`
- Test: `iterm/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
    # --- discussions --------------------------------------------------------
    run_cli("join", "d-a", "--project", "dp", iterm_id="w0t1p0:D-A")
    run_cli("join", "d-b", "--project", "dp", iterm_id="w0t1p0:D-B")
    code, out, err = run_cli("discuss", "d-b", "one DB or many?",
                             iterm_id="w0t1p0:D-A")
    ok &= check("discuss exits 0", code == 0)
    c = db.connect()
    tid = c.execute("SELECT id FROM threads ORDER BY id DESC "
                    "LIMIT 1").fetchone()[0]
    c.close()
    ok &= check("discuss prints the thread id", str(tid) in out)
    ok &= check("discuss queues to the peer",
                _one_message("d-b")["thread_id"] == tid)

    code, out, err = run_cli("discuss", "nobody-here", "topic",
                             iterm_id="w0t1p0:D-A")
    ok &= check("unknown peer is refused", code != 0)
    ok &= check("unknown peer names relay who", "relay who" in err)

    code, out, err = run_cli("say", str(tid), "one per service",
                             iterm_id="w0t1p0:D-B")
    ok &= check("say exits 0", code == 0)
    code, out, err = run_cli("thread", str(tid), iterm_id="w0t1p0:D-B")
    ok &= check("thread shows the transcript", "one per service" in out)
    ok &= check("thread shows the topic", "one DB or many?" in out)
    ok &= check("thread shows rounds left", "round" in out.lower())

    code, out, err = run_cli("agree", str(tid), "", iterm_id="w0t1p0:D-B")
    ok &= check("empty agreement is refused", code != 0)
    code, out, err = run_cli("agree", str(tid), "one per service",
                             iterm_id="w0t1p0:D-B")
    ok &= check("agree exits 0", code == 0)

    # cap enforcement
    for i in range(5):
        run_cli("say", str(tid), f"post {i}", iterm_id="w0t1p0:D-A")
    code, out, err = run_cli("say", str(tid), "one too many",
                             iterm_id="w0t1p0:D-A")
    ok &= check("say past the cap is refused", code != 0)
    ok &= check("cap refusal offers agree", "relay agree" in err)

    # a non-participant cannot post
    run_cli("join", "d-outsider", "--project", "dp",
            iterm_id="w0t1p0:D-OUT")
    code, out, err = run_cli("say", str(tid), "butting in",
                             iterm_id="w0t1p0:D-OUT")
    ok &= check("a non-participant cannot post", code != 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - "invalid choice: 'discuss'"

- [ ] **Step 3: Write minimal implementation**

Add to `iterm/cli.py`:

```python
def _thread_or_err(conn, tid, me):
    """(thread_row, 0) or (None, exit_code). Every thread verb needs the same
    three checks, and each failure teaches the way out."""
    th = db.get_thread(conn, tid)
    if th is None:
        return None, _err(f"no discussion #{tid} - relay thread <id> lists "
                          f"one, relay who lists sessions")
    if th["state"] != "open":
        return None, _err(f"discussion #{tid} is closed ({th['state']}): "
                          f"{th['outcome'] or '(no outcome recorded)'}")
    if me["name"] not in db.participants_of(th):
        return None, _err(f"you are not in discussion #{tid} - its "
                          f"participants are "
                          f"{', '.join(db.participants_of(th))}")
    return th, 0


def _broadcast(conn, th, sender, body, kind):
    """Post to every participant except the sender. One message row each, so
    delivery, batching and the inbox all keep working unchanged."""
    tid = th["id"]
    for p in db.participants_of(th):
        if p == sender:
            continue
        db.queue_message(conn, sender, p, body, th["project"], kind=kind,
                         thread_id=tid)


def cmd_discuss(args) -> int:
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    peers = list(args.peers or ())
    if len(peers) < 2:
        return _err('usage: relay discuss <name> [<name>...] "<topic>"')
    topic = peers.pop()          # last positional is the topic
    if not topic.strip():
        return _err("the topic cannot be empty")
    if args.rounds < 1 or args.rounds > 10:
        return _err("--rounds must be between 1 and 10 (every round costs "
                    "each participant a full Claude turn)")
    seen, names = set(), []
    for p in peers:
        if p == me["name"] or p in seen:
            continue
        seen.add(p)
        if p in db.RESERVED_NAMES:
            return _err(f"'{p}' is not a session you can talk to")
        s = db.get_session(conn, p)
        if s is None or s["closed_at"]:
            return _err(f"unknown session '{p}' - relay who lists who is here")
        names.append(p)
    if not names:
        return _err("name at least one other session to discuss this with "
                    "- relay who lists them")
    tid = db.create_thread(conn, topic.strip(), me["name"], names,
                           project=me["project"], rounds_cap=args.rounds)
    th = db.get_thread(conn, tid)
    _broadcast(conn, th, me["name"], topic.strip(), "say")
    print(f"opened discussion #{tid} with {', '.join(names)}")
    print(f"topic: {topic.strip()}")
    print(f"each participant may post {args.rounds} time(s); settle with: "
          f'relay agree {tid} "<the position>"')
    return 0


def cmd_say(args) -> int:
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    th, rc = _thread_or_err(conn, args.id, me)
    if th is None:
        return rc
    if not args.body.strip():
        return _err("say something - an empty post is not a position")
    msgs = db.thread_messages(conn, th["id"])
    used = swarm.round_counts(msgs).get(me["name"], 0)
    if used >= th["rounds_cap"]:
        return _err(f"you have used all {th['rounds_cap']} of your posts in "
                    f"#{th['id']}. Either settle - relay agree {th['id']} "
                    f'"<the position>" - or stop and let it close.')
    _broadcast(conn, th, me["name"], args.body.strip(), "say")
    left = th["rounds_cap"] - used - 1
    print(f"posted to #{th['id']} ({left} post(s) left)")
    return 0


def cmd_agree(args) -> int:
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    th, rc = _thread_or_err(conn, args.id, me)
    if th is None:
        return rc
    position = (args.position or "").strip()
    if not position:
        return _err("state WHAT you are agreeing to: relay agree "
                    f'{th["id"]} "<the position>". Three sessions agreeing '
                    f"while describing three different things is the failure "
                    f"this catches.")
    _broadcast(conn, th, me["name"], position, "agree")
    print(f"recorded your position on #{th['id']}: {position}")
    print("(posting again with relay say retracts it)")
    return 0


def cmd_thread(args) -> int:
    """The read path. Everything a participant needs in one place, ending
    with what THIS session can do right now."""
    conn = db.connect()
    me = whoami(conn)
    th = db.get_thread(conn, args.id)
    if th is None:
        return _err(f"no discussion #{args.id}")
    parts = db.participants_of(th)
    msgs = db.thread_messages(conn, th["id"])
    print(f"DISCUSSION #{th['id']}  [{th['state']}]")
    print(f"topic: {th['topic']}")
    print(f"with:  {', '.join(parts)}")
    print()
    print("TRANSCRIPT")
    if not msgs:
        print("  (nothing posted yet)")
    for m in msgs:
        mark = "AGREES:" if swarm.kind_of(m) == "agree" else "       "
        print(f"  {m['from_name']:<14} {mark} {m['body']}")
    print()
    pos = swarm.positions(msgs)
    counts = swarm.round_counts(msgs)
    print("POSITIONS")
    for p in parts:
        if p in pos:
            print(f"  {p:<14} settled: {pos[p]}")
        else:
            print(f"  {p:<14} not settled "
                  f"({th['rounds_cap'] - counts.get(p, 0)} post(s) left)")
    if th["state"] != "open":
        print()
        print(f"OUTCOME ({th['state']}): {th['outcome']}")
        return 0
    print()
    print("STATE YOUR POSITION AND SAY WHERE YOU DISAGREE. Do not aim for "
          "consensus;")
    print("aim to be right. An honest deadlock is a useful outcome here.")
    if me is not None and me["name"] in parts:
        left = th["rounds_cap"] - counts.get(me["name"], 0)
        print()
        print("YOU CAN:")
        if left > 0:
            print(f'  relay say {th["id"]} "<your view>"        '
                  f'({left} post(s) left)')
        else:
            print("  (no posts left)")
        print(f'  relay agree {th["id"]} "<the position>"   settle it')
    return 0
```

Parser entries:

```python
    dc = sub.add_parser("discuss", help="open a discussion with other "
                                        "sessions and settle a question")
    dc.add_argument("peers", nargs="*",
                    help="peer names, then the topic as the last argument")
    dc.add_argument("--rounds", type=int, default=3,
                    help="posts allowed per participant (default 3)")
    dc.set_defaults(fn=cmd_discuss)

    sy = sub.add_parser("say", help="post to a discussion")
    sy.add_argument("id", type=int)
    sy.add_argument("body")
    sy.set_defaults(fn=cmd_say)

    ag = sub.add_parser("agree", help="record the position you are settled on")
    ag.add_argument("id", type=int)
    ag.add_argument("position")
    ag.set_defaults(fn=cmd_agree)

    tr = sub.add_parser("thread", help="read a discussion")
    tr.add_argument("id", type=int)
    tr.set_defaults(fn=cmd_thread)
```

Add `discuss|say|agree|thread` to `bin/relay`'s dispatch alternation and `-h`.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py bin/relay
git commit -m "feat(cli): relay discuss|say|agree|thread"
```

---

### Task 12: Thread pointer in the delivery envelope

**Files:**
- Modify: `iterm/swarm.py` (`batch_delivery_text`)
- Test: `iterm/test_swarm.py`

- [ ] **Step 1: Write the failing test**

```python
    # --- thread pointers ----------------------------------------------------
    tp = [{"id": 9, "from_name": "api", "body": "one per service",
           "kind": "say", "thread_id": 7}]
    t = swarm.batch_delivery_text(tp)
    ok &= check("thread delivery points at relay thread",
                "relay thread 7" in t)
    ok &= check("thread pointer does not say reply", "relay reply" not in t)
    ok &= check("thread pointer names the sender", "api" in t)
    ok &= check("thread pointer is one line", "\n" not in t)
    mixed = tp + [{"id": 10, "from_name": "bff", "body": "shared",
                   "kind": "say", "thread_id": 7}]
    ok &= check("several thread posts collapse to one pointer",
                swarm.batch_delivery_text(mixed).count("relay thread 7") == 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL - the pointer text is absent.

- [ ] **Step 3: Write minimal implementation**

At the top of `batch_delivery_text`, before the single/batch split:

```python
    tids = []
    for m in msgs:
        t = _get(m, "thread_id", None)
        if t and t not in tids:
            tids.append(t)
    if tids and len(tids) == 1 and all(_get(m, "thread_id", None)
                                       for m in msgs):
        tid = tids[0]
        senders = []
        for m in msgs:
            s = _get(m, "from_name")
            if s not in senders:
                senders.append(s)
        n = len(msgs)
        # A POINTER, not the payload. The transcript arrives as bash output
        # from `relay thread`, where it can be multi-line and unabridged -
        # injected text is one flattened line, and a three-way transcript
        # flattened onto one line is unreadable. Deliberately does NOT say
        # "reply": a thread is posted to with `relay say`, and naming the
        # wrong verb in the one line a woken session is guaranteed to read is
        # the most expensive wording mistake available here.
        return _clip(f"[relay discussion #{tid}] {n} new post(s) from "
                     f"{', '.join(senders)} - read them first: "
                     f"relay thread {tid}")
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): discussions deliver a pointer, not the payload"
```

---

### Task 13: The watcher closes threads

**Files:**
- Modify: `iterm/watcher.py` (new `_close_threads`, called from the same
  place as `_check_escalations`)
- Test: `iterm/test_watcher.py`

- [ ] **Step 1: Write the failing test**

Create an open thread with two participants, insert an `agree` from each, run
the sweep, and assert the thread is `agreed`, that a message to `human` with
kind `escalation` now exists carrying the outcome, and that running the sweep
a second time creates no second message.

Then a capped thread: `rounds_cap=1`, one `say` from each participant, no
agreement - assert `unresolved` and one ping.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL - the thread stays `open`.

- [ ] **Step 3: Write minimal implementation**

```python
    def _close_threads(self) -> None:
        """Close discussions that have reached a verdict, and ping the
        operator with the outcome. Runs in dry-run too: queueing a message and
        stamping a thread are DB writes, not keystrokes, and the operator
        wanting to know how a discussion ended does not depend on relay being
        allowed to type. Best-effort - never breaks the loop."""
        try:
            conn = self._swarm_conn()
            for th in swarmdb.list_threads(conn, state="open"):
                parts = swarmdb.participants_of(th)
                msgs = swarmdb.thread_messages(conn, th["id"])
                state, outcome = swarm.thread_verdict(
                    parts, msgs, th["rounds_cap"])
                if state == "open":
                    continue
                # Guarded on state='open' inside close_thread, so two ticks
                # racing cannot double-ping.
                if not swarmdb.close_thread(conn, th["id"], state, outcome):
                    continue
                verdict = ("agreed" if state == "agreed"
                           else "could not agree")
                swarmdb.queue_message(
                    conn, "relay", "human",
                    f"discussion #{th['id']} ({th['topic']}) {verdict}: "
                    f"{outcome}",
                    th["project"], kind="escalation", thread_id=th["id"])
                self._note(f"THREAD #{th['id']} {state}: {outcome[:80]}")
        except Exception as e:
            self._note(f"thread close error: {e}")
```

Call it immediately after `self._check_escalations()` in the tick.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "feat(watcher): close discussions on a verdict and ping the human"
```

---

### Task 14: Wipe, prune and doctor know about threads

**Files:**
- Modify: `iterm/db.py:632-660` (`wipe_project`, `prune_messages` area),
  `iterm/cli.py` (`cmd_doctor`)
- Test: `iterm/test_db.py`, `iterm/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
    # test_db.py
    ok &= check("wipe_project removes its threads",
                db.get_thread(conn, wiped_tid) is None)
```

```python
    # test_cli.py
    code, out, err = run_cli("doctor")
    ok &= check("doctor reports open discussions",
                "discussion" in out.lower())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test/run.sh`
Expected: FAIL on both.

- [ ] **Step 3: Write minimal implementation**

In `wipe_project`, add alongside the existing deletes:

```python
    conn.execute("DELETE FROM threads WHERE project=?", (project,))
```

In `cmd_doctor`, add a section modeled on the existing PR health block:

```python
    open_threads = db.list_threads(conn, state="open")
    print(f"discussions: {len(open_threads)} open")
    for th in open_threads:
        parts = db.participants_of(th)
        settled = len(swarm.positions(db.thread_messages(conn, th["id"])))
        print(f"  #{th['id']} {th['topic'][:48]} "
              f"({settled}/{len(parts)} settled, "
              f"{_ago(th['created_at'])})")
```

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/cli.py iterm/test_db.py iterm/test_cli.py
git commit -m "feat: wipe and doctor account for discussions"
```

---

### Task 15: `relay help discuss` and the docs

**Files:**
- Modify: `iterm/protocol.py`, `skills/relay-worker/SKILL.md`,
  `skills/relay-cli-reference.md`, `README.md`, `bin/relay`
- Test: `iterm/test_protocol.py`

- [ ] **Step 1: Write the failing test**

```python
    ok &= check("discuss is a help topic", "discuss" in protocol.TOPICS)
    d = protocol.TOPICS["discuss"]
    ok &= check("discuss protocol names the verbs",
                all(v in d for v in ("relay discuss", "relay say",
                                     "relay agree", "relay thread")))
    ok &= check("discuss protocol warns against consensus-seeking",
                "disagree" in d.lower())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_protocol.py`
Expected: FAIL - `'discuss' not in TOPICS`

- [ ] **Step 3: Write minimal implementation**

Add `DISCUSS_PROTOCOL` to `iterm/protocol.py` covering: what a discussion is,
the four verbs, that `relay thread <id>` must be read before posting, that the
round cap is real and `agree` does not consume one, that a `say` after an
`agree` retracts it, and - stated plainly - that participants should state a
position and name their disagreements rather than seek consensus. Register it:

```python
TOPICS = {"swarm": SWARM_PROTOCOL, "pr": PR_PROTOCOL,
          "discuss": DISCUSS_PROTOCOL}
```

Then:
- `bin/relay`: rewrite the `-h` block so it LEADS with talking
  (`relay join`, `relay who`, `relay discuss`) before the operational verbs,
  and add every new verb to the dispatch alternation.
- `skills/relay-worker/SKILL.md`: cut the reply-ceremony prose that
  `relay reply` now handles, and add a short discussions section pointing at
  `relay help discuss` rather than restating it.
- `skills/relay-cli-reference.md` and `README.md`: document the new verbs.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/protocol.py iterm/test_protocol.py bin/relay skills README.md
git commit -m "docs: relay help discuss, and -h leads with talking"
```

---

### Task 16: DISCUSSIONS pane in the swarm view

**Files:**
- Modify: `iterm/app.py` (swarm view render)
- Test: `iterm/test_app.py`

Follow the PR pane added in commit `3501e9b` as the template, including its
empty-state behavior (fixed in `2b73fc4`: the pane renders when empty rather
than vanishing).

- [ ] **Step 1: Write the failing test**

```python
    ok &= check("swarm view renders a DISCUSSIONS pane when empty",
                "DISCUSSIONS" in render_swarm_view(no_threads_state))
    ok &= check("an open discussion shows its topic",
                "one DB or many?" in render_swarm_view(one_thread_state))
    ok &= check("a closed discussion shows its verdict",
                "agreed" in render_swarm_view(closed_thread_state))
```

Match the existing helper names in `iterm/test_app.py` for building view
state - do not invent a new harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL - no DISCUSSIONS pane.

- [ ] **Step 3: Write minimal implementation**

Render a pane listing open discussions (id, topic clipped to the column,
`settled/total`, age) above a stable list, matching the PR pane's structure.
Threads needing attention (closed but not yet seen by the operator) go in the
attention strip on top; **the main list order never changes** - it is ordered
by thread id, ascending, always.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): DISCUSSIONS pane in the swarm view"
```

---

# PHASE 3 - synchronous ask

Additive. Defer freely if phases 1 and 2 prove sufficient.

---

### Task 17: `relay ask --wait`

**Files:**
- Modify: `iterm/db.py` (add `find_reply`), `iterm/cli.py` (add `cmd_ask`),
  `bin/relay`
- Test: `iterm/test_db.py`, `iterm/test_cli.py`

**Interfaces:**
- Produces: `db.find_reply(conn, to_name, ask_id, peer, since) -> Optional[Row]`

- [ ] **Step 1: Write the failing test**

```python
    # test_db.py
    aid = db.queue_message(conn, "x", "y", "question?", kind="ask")
    ok &= check("no reply yet",
                db.find_reply(conn, "x", aid, "y", 0) is None)
    db.queue_message(conn, "y", "x", "answer!", reply_to=aid)
    ok &= check("correlated reply is found",
                db.find_reply(conn, "x", aid, "y", 0)["body"] == "answer!")
    aid2 = db.queue_message(conn, "x", "z", "q2?", kind="ask", now=100.0)
    db.queue_message(conn, "z", "x", "sloppy answer", now=200.0)
    ok &= check("an uncorrelated reply from the peer still counts",
                db.find_reply(conn, "x", aid2, "z", 100.0)["body"]
                == "sloppy answer")
```

```python
    # test_cli.py - the non-blocking paths only; do not sleep in the suite
    code, out, err = run_cli("ask", "nobody", "q", "--wait", "0",
                             iterm_id="w0t1p0:ASK-A")
    ok &= check("ask to an unknown session is refused", code != 0)
    run_cli("join", "ask-b", iterm_id="w0t1p0:ASK-B")
    code, out, err = run_cli("ask", "ask-b", "q", "--wait", "0",
                             iterm_id="w0t1p0:ASK-A")
    ok &= check("ask with no answer times out non-zero", code != 0)
    ok &= check("timeout explains the async fallback", "queued" in err)
    ok &= check("the question is still queued",
                _one_message("ask-b")["kind"] == "ask")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test/run.sh`
Expected: FAIL - `find_reply` missing, "invalid choice: 'ask'"

- [ ] **Step 3: Write minimal implementation**

`iterm/db.py`:

```python
def find_reply(conn, to_name: str, ask_id: int, peer: str,
               since: float) -> Optional[sqlite3.Row]:
    """The answer to `ask_id`, correlated, with a forgiving fallback.

    Strict correlation first. But a peer that answers with a plain
    `relay send` instead of `relay reply` has still answered, and hanging
    until timeout because it used the wrong verb would make the feature look
    broken when it was merely informal."""
    row = conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND reply_to=? "
        "ORDER BY id LIMIT 1", (to_name, ask_id)).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND from_name=? "
        "AND created_at > ? ORDER BY id LIMIT 1",
        (to_name, peer, since)).fetchone()
```

`iterm/cli.py`:

```python
_ASK_WAIT_MAX = 540      # under Claude Code's bash timeout ceiling (600s)
_ASK_POLL_S = 0.5


def cmd_ask(args) -> int:
    """Ask a peer and block until it answers, so a question does not cost the
    asker a turn boundary. NOT a discussion: no rounds, no agreement, no
    threads row - routing it through the discussion object would hand it
    closing semantics it cannot satisfy (nobody posts `agree` to a question),
    and every successful ask would eventually close as `unresolved`."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    peer = db.get_session(conn, args.to)
    if peer is None or peer["closed_at"]:
        return _err(f"unknown session '{args.to}' - relay who lists who "
                    f"is here")
    if args.to == me["name"]:
        return _err("you cannot ask yourself")
    wait = max(0, min(int(args.wait), _ASK_WAIT_MAX))
    started = time.time()
    aid = db.queue_message(conn, me["name"], args.to, args.body,
                           me["project"], kind="ask")
    deadline = started + wait
    while True:
        row = db.find_reply(conn, me["name"], aid, args.to, started)
        if row is not None:
            db.mark_delivered(conn, row["id"])
            print(f"{args.to}: {row['body']}")
            return 0
        if time.time() >= deadline:
            break
        time.sleep(_ASK_POLL_S)
    return _err(f"no answer from {args.to} within {wait}s - your question is "
                f"queued and will be delivered when they are idle. End your "
                f"turn; relay wakes you with the reply.")
```

Parser:

```python
    ak = sub.add_parser("ask", help="ask a session and wait for the answer")
    ak.add_argument("to")
    ak.add_argument("body")
    ak.add_argument("--wait", type=int, default=120,
                    help="seconds to block (default 120, max 540)")
    ak.set_defaults(fn=cmd_ask)
```

Add `ask` to `bin/relay`'s dispatch alternation and `-h` block, and to
`DISCUSS_PROTOCOL`.

- [ ] **Step 4: Run the full suite**

Run: `./test/run.sh`
Expected: ALL SUITES PASSED

- [ ] **Step 5: Commit**

```bash
git add iterm/db.py iterm/cli.py iterm/test_db.py iterm/test_cli.py bin/relay
git commit -m "feat(cli): relay ask - a question that does not cost a turn"
```

---

## Self-Review Notes

**Spec coverage:** §2 identity → Tasks 2, 3, 5. §3 discovery → Task 4. §4
discussions → Tasks 9, 10, 11. §5 delivery → Tasks 6, 7, 12. §5 closing →
Task 13. §6 ask/reply → Tasks 8, 17. §7 anti-sycophancy → Tasks 11
(`cmd_thread` framing, non-empty `agree`), 15 (`DISCUSS_PROTOCOL`). §8
surfacing → Tasks 14, 16. §9 learnability → Tasks 4, 5, 11, 15. §11 non-goals
→ nothing built.

**Spec corrections made here:** name derivation drops the tab-title source
(unavailable to the CLI) and uses the cwd basename only (Task 2). Migration key
is 9 with `_CURRENT_VERSION` 10 (Task 1).

**Known follow-ups not in this plan:** `prune_messages` does not touch
`threads`; closed threads accumulate. Harmless at this scale, and `wipe`
clears them. Revisit if a DB ever holds thousands.
