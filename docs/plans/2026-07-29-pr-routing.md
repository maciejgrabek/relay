# PR Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relay answers "who owns this pull request?" so PR review feedback routes automatically to the session that opened the PR, or escalates to the human when it cannot.

**Architecture:** One new SQLite table (`prs`) keyed on `(repo, number)`, storing the owner's session name *and* its iTerm session id so a rebound name cannot be mistaken for the original author. Pure resolution logic lives in `swarm.py` (no sqlite, no iTerm2 imports, testable headless); the CLI wraps it in `relay pr set|claim|list` and `relay send --pr`. A new reserved recipient `human` carries escalations that ping the operator but are never injected into any session. The swarm view gains a PR pane.

**Tech Stack:** Python 3 stdlib only (sqlite3, argparse), Rich markup for TUI text, Textual for the app shell. No pytest: every suite is a `__main__` runner wired into `test/run.sh`.

## Global Constraints

- **Relay stays gh-less.** No `gh` invocation, no GitHub HTTP call, no repo list anywhere in this codebase. Every PR fact is pushed in by a session.
- **Stored PR state is last-known-as-reported.** Any UI that shows a state MUST show the age of that report beside it.
- **No em-dash characters (U+2014) anywhere** - source, comments, docs, commit messages. Use a plain ASCII hyphen.
- **Commit messages carry no `Co-Authored-By` trailer.**
- New tables go in `db._SCHEMA` as `CREATE TABLE IF NOT EXISTS` with **no migration and no `user_version` bump** (the idiom documented at `db.py:85-92`). Indexes go in the separate index block that runs after `_migrate`, never inside `_SCHEMA`.
- Every dynamic string rendered into the TUI goes through `swarm._esc()` before it reaches Rich markup.
- Exit codes: `0` ok, `1` user/state error, `2` argparse usage, and this plan adds `3` unclaimed and `4` owner gone.
- PR reference format is `owner/name#number` everywhere, as a positional argument. Never a `--repo` flag.
- Valid PR states, in order: `created`, `review`, `changes`, `approved`, `merged`, `closed`.

---

### Task 1: `prs` table and DB layer

**Files:**
- Modify: `iterm/db.py` (add `PR_STATES` and `RESERVED_NAMES` constants near `MESSAGE_KINDS:27`, the table in `_SCHEMA:29`, the index in the index block after `_migrate`, and new functions after `prune_messages:606`)
- Test: `iterm/test_db.py` (new section before `conn.close()`)

**Interfaces:**
- Consumes: `db.connect`, `db._now` (existing).
- Produces: `db.PR_STATES`, `db.RESERVED_NAMES`, `db.upsert_pr(conn, repo, number, *, project=None, state=None, title=None, branch=None, now=None) -> sqlite3.Row`, `db.claim_pr(conn, repo, number, *, owner, owner_session_id, task_id=None, branch=None, project=None, now=None) -> sqlite3.Row`, `db.get_pr(conn, repo, number) -> Optional[sqlite3.Row]`, `db.list_prs(conn, project=None, owner=None, since=None) -> List[sqlite3.Row]`, `db.touch_pr_routed(conn, repo, number, now=None) -> None`, `db.prune_prs(conn, older_than_days, now=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `iterm/test_db.py`, immediately before the final `conn.close()`:

```python
    # --- prs ----------------------------------------------------------------
    row = db.upsert_pr(conn, "acme/api", 482, project="webshop",
                       state="created", title="Add rate limiting",
                       branch="relay/api-worker", now=1000.0)
    ok &= check("upsert_pr creates a row with no owner",
                row["repo"] == "acme/api" and row["number"] == 482
                and row["owner"] == "" and row["state"] == "created")
    ok &= check("upsert_pr stamps state_changed_at and updated_at",
                row["state_changed_at"] == 1000.0
                and row["updated_at"] == 1000.0)

    same = db.upsert_pr(conn, "acme/api", 482, state="created", now=2000.0)
    ok &= check("re-upsert with the SAME state moves updated_at only",
                same["updated_at"] == 2000.0
                and same["state_changed_at"] == 1000.0)

    moved = db.upsert_pr(conn, "acme/api", 482, state="changes", now=3000.0)
    ok &= check("upsert with a NEW state re-stamps state_changed_at",
                moved["state"] == "changes"
                and moved["state_changed_at"] == 3000.0)
    ok &= check("upsert preserves fields it was not given",
                moved["title"] == "Add rate limiting"
                and moved["branch"] == "relay/api-worker")

    ok &= check("upsert_pr rejects an unknown state", _raises(
        lambda: db.upsert_pr(conn, "acme/api", 482, state="merged-ish")))

    claimed = db.claim_pr(conn, "acme/api", 482, owner="api-worker",
                          owner_session_id="SID-A", task_id=14, now=4000.0)
    ok &= check("claim_pr records owner, session id and task",
                claimed["owner"] == "api-worker"
                and claimed["owner_session_id"] == "SID-A"
                and claimed["task_id"] == 14
                and claimed["claimed_at"] == 4000.0)
    ok &= check("claim_pr does not disturb state",
                claimed["state"] == "changes")

    fresh = db.claim_pr(conn, "acme/web", 31, owner="fe-worker",
                        owner_session_id="SID-B", project="webshop",
                        now=4100.0)
    ok &= check("claim_pr creates the row when the sweep never saw the PR",
                fresh["number"] == 31 and fresh["state"] == "created")

    reclaim = db.claim_pr(conn, "acme/api", 482, owner="api-worker",
                          owner_session_id="SID-C", now=5000.0)
    ok &= check("re-claiming overwrites the session id (restore case)",
                reclaim["owner_session_id"] == "SID-C")

    ok &= check("get_pr finds by ref",
                db.get_pr(conn, "acme/api", 482)["id"] == claimed["id"])
    ok &= check("get_pr returns None for an unknown ref",
                db.get_pr(conn, "acme/api", 999) is None)

    ok &= check("list_prs is ordered by repo then number",
                [(r["repo"], r["number"]) for r in db.list_prs(conn)]
                == [("acme/api", 482), ("acme/web", 31)])
    ok &= check("list_prs --owner filters",
                [r["number"] for r in db.list_prs(conn, owner="fe-worker")]
                == [31])
    ok &= check("list_prs --since filters on updated_at",
                [r["number"] for r in db.list_prs(conn, since=4050.0)] == [31])

    db.touch_pr_routed(conn, "acme/api", 482, now=6000.0)
    ok &= check("touch_pr_routed stamps last_routed_at",
                db.get_pr(conn, "acme/api", 482)["last_routed_at"] == 6000.0)

    # retention: merged/closed prune, open never does, at any age
    db.upsert_pr(conn, "acme/api", 400, state="merged", now=1.0)
    db.upsert_pr(conn, "acme/api", 401, state="closed", now=1.0)
    db.upsert_pr(conn, "acme/api", 402, state="review", now=1.0)
    n = db.prune_prs(conn, 7, now=1.0 + 8 * 86400)
    ok &= check("prune_prs drops old merged and closed rows", n == 2)
    ok &= check("prune_prs never drops an open PR, however old",
                db.get_pr(conn, "acme/api", 402) is not None)

    ok &= check("RESERVED_NAMES covers relay and human",
                set(db.RESERVED_NAMES) == {"relay", "human"})
```

Add this helper next to `check()` near the top of `iterm/test_db.py` (it does not exist yet):

```python
def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_db.py`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'upsert_pr'`

- [ ] **Step 3: Add the constants and the table**

In `iterm/db.py`, after `MESSAGE_KINDS` (line 27):

```python
PR_STATES = ("created", "review", "changes", "approved", "merged", "closed")

# Names no session may register. 'relay' is the sender of system wake-ups;
# 'human' is the recipient of operator escalations, which must never resolve
# to a tab that could be injected into.
RESERVED_NAMES = ("relay", "human")
```

Inside the `_SCHEMA` string, after the `timers` table:

```sql
CREATE TABLE IF NOT EXISTS prs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'created',
  task_id INTEGER,
  owner TEXT NOT NULL DEFAULT '',
  owner_session_id TEXT NOT NULL DEFAULT '',
  claimed_at REAL NOT NULL DEFAULT 0,
  state_changed_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  last_routed_at REAL NOT NULL DEFAULT 0
);
```

In the index block that runs after `_migrate` (see the comment at `db.py:85`), add:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_prs_ref ON prs(repo, number);
```

Do NOT add a migration and do NOT bump `user_version`. A new `CREATE TABLE IF NOT EXISTS` is a no-op on existing DBs, which is exactly why the idiom exists.

- [ ] **Step 4: Add the DB functions**

Append to `iterm/db.py` after `prune_messages`:

```python
def _pr_row(conn, repo: str, number: int):
    return conn.execute(
        "SELECT * FROM prs WHERE repo = ? AND number = ?",
        (repo, int(number))).fetchone()


def get_pr(conn, repo: str, number: int) -> Optional[sqlite3.Row]:
    return _pr_row(conn, repo, number)


def upsert_pr(conn, repo: str, number: int, *, project=None, state=None,
              title=None, branch=None, now=None) -> sqlite3.Row:
    """Create or update a PR row. Only the fields passed are written, so a
    sweep pushing state cannot blank a title a worker recorded. Changing the
    state re-stamps state_changed_at; every call moves updated_at."""
    if state is not None and state not in PR_STATES:
        raise ValueError(f"unknown PR state {state!r}; "
                         f"expected one of {', '.join(PR_STATES)}")
    ts = _now(now)
    cur = _pr_row(conn, repo, number)
    if cur is None:
        conn.execute(
            "INSERT INTO prs(project, repo, number, title, branch, state,"
            " state_changed_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (project or "", repo, int(number), title or "", branch or "",
             state or "created", ts, ts))
    else:
        new_state = state or cur["state"]
        sc = ts if new_state != cur["state"] else cur["state_changed_at"]
        conn.execute(
            "UPDATE prs SET project = ?, title = ?, branch = ?, state = ?,"
            " state_changed_at = ?, updated_at = ? WHERE id = ?",
            (project if project is not None else cur["project"],
             title if title is not None else cur["title"],
             branch if branch is not None else cur["branch"],
             new_state, sc, ts, cur["id"]))
    conn.commit()
    return _pr_row(conn, repo, number)


def claim_pr(conn, repo: str, number: int, *, owner: str,
             owner_session_id: str, task_id=None, branch=None, project=None,
             now=None) -> sqlite3.Row:
    """Attach ownership. Creates the row when the sweep has not seen the PR
    yet. Re-claiming overwrites owner_session_id: a restored worker resuming
    its own PR is the case this must support."""
    ts = _now(now)
    if _pr_row(conn, repo, number) is None:
        upsert_pr(conn, repo, number, project=project, branch=branch, now=ts)
    cur = _pr_row(conn, repo, number)
    conn.execute(
        "UPDATE prs SET owner = ?, owner_session_id = ?, task_id = ?,"
        " branch = ?, project = ?, claimed_at = ?, updated_at = ?"
        " WHERE id = ?",
        (owner, owner_session_id,
         task_id if task_id is not None else cur["task_id"],
         branch if branch is not None else cur["branch"],
         project if project is not None else cur["project"],
         ts, ts, cur["id"]))
    conn.commit()
    return _pr_row(conn, repo, number)


def list_prs(conn, project=None, owner=None,
             since=None) -> List[sqlite3.Row]:
    """Stable order: repo, then number. Never sorted by urgency - the TUI
    duplicates what needs attention into a strip above the list instead."""
    q = "SELECT * FROM prs WHERE 1=1"
    p: list = []
    if project:
        q += " AND project = ?"
        p.append(project)
    if owner:
        q += " AND owner = ?"
        p.append(owner)
    if since is not None:
        q += " AND updated_at >= ?"
        p.append(float(since))
    return conn.execute(q + " ORDER BY repo, number", p).fetchall()


def touch_pr_routed(conn, repo: str, number: int, now=None) -> None:
    conn.execute(
        "UPDATE prs SET last_routed_at = ? WHERE repo = ? AND number = ?",
        (_now(now), repo, int(number)))
    conn.commit()


def prune_prs(conn, older_than_days: float, now=None) -> int:
    """Drop settled PRs past the retention window. An open PR is live state,
    never history: it is kept at any age, mirroring the rule that queued
    messages survive prune_messages."""
    cutoff = _now(now) - older_than_days * 86400
    cur = conn.execute(
        "DELETE FROM prs WHERE state IN ('merged','closed') AND updated_at < ?",
        (cutoff,))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_db.py`
Expected: PASS, ending in `ALL PASS`. The existing assertion `fresh connect stamps user_version = 8` must still pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add iterm/db.py iterm/test_db.py
git commit -m "feat(db): prs table - who owns which pull request"
```

---

### Task 2: PR reference parsing and route resolution

**Files:**
- Modify: `iterm/swarm.py` (add near the other pure helpers, after `stale_reason`)
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Consumes: `swarm._get` (existing tolerant field accessor).
- Produces: `swarm.parse_pr_ref(ref) -> Optional[tuple[str, int]]`, `swarm.resolve_pr_route(pr, session) -> tuple[str, str]` where the first element is one of `"ok"`, `"unclaimed"`, `"gone"` and the second is the owner name (for `"ok"`) or a human-readable reason.

- [ ] **Step 1: Write the failing tests**

Append to the `run()` body of `iterm/test_swarm.py`, before its final summary print:

```python
    # --- PR ref parsing -----------------------------------------------------
    ok &= check("parse_pr_ref splits owner/name#number",
                swarm.parse_pr_ref("acme/api#482") == ("acme/api", 482))
    ok &= check("parse_pr_ref accepts dots and dashes in the repo",
                swarm.parse_pr_ref("my-org/api.core#7")
                == ("my-org/api.core", 7))
    for bad in ("acme/api", "482", "acme/api#", "#482", "acme#482",
                "acme/api#abc", "a/b#1#2", "", "acme/api#-1"):
        ok &= check(f"parse_pr_ref rejects {bad!r}",
                    swarm.parse_pr_ref(bad) is None)

    # --- route resolution ---------------------------------------------------
    live = {"name": "api-worker", "iterm_session_id": "SID-A", "closed_at": 0}
    pr = {"owner": "api-worker", "owner_session_id": "SID-A"}
    ok &= check("routable when the owner session is the claiming session",
                swarm.resolve_pr_route(pr, live) == ("ok", "api-worker"))

    ok &= check("no row at all is unclaimed",
                swarm.resolve_pr_route(None, None)[0] == "unclaimed")
    ok &= check("a row the sweep pushed but nobody claimed is unclaimed",
                swarm.resolve_pr_route(
                    {"owner": "", "owner_session_id": ""}, None)[0]
                == "unclaimed")

    st, why = swarm.resolve_pr_route(pr, None)
    ok &= check("owner name no longer registered is gone", st == "gone")
    ok &= check("gone reason names the missing session",
                "api-worker" in why)

    st, why = swarm.resolve_pr_route(
        pr, {"name": "api-worker", "iterm_session_id": "SID-A",
             "closed_at": 123.0})
    ok &= check("closed owner session is gone", st == "gone")
    ok &= check("gone reason says closed", "closed" in why)

    # The bug owner_session_id exists to prevent: the name was reclaimed by a
    # different tab, which never saw this branch.
    st, why = swarm.resolve_pr_route(
        pr, {"name": "api-worker", "iterm_session_id": "SID-Z",
             "closed_at": 0})
    ok &= check("name rebound to a different tab is gone, NOT routable",
                st == "gone")
    ok &= check("gone reason says rebound", "rebound" in why)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL with `AttributeError: module 'swarm' has no attribute 'parse_pr_ref'`

- [ ] **Step 3: Implement both functions**

In `iterm/swarm.py`, after `stale_reason`:

```python
# owner/name#number - the single PR reference format, used by every verb and
# every message. One format is one less thing for a session to get wrong.
_PR_REF_RE = re.compile(r"^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)$")


def parse_pr_ref(ref: str):
    """('acme/api', 482) or None. Strict: a caller that guesses at the format
    gets an error, not a row written under a ref nothing will ever match."""
    m = _PR_REF_RE.match((ref or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def resolve_pr_route(pr, session):
    """Can PR feedback be routed to the session that opened it?

    Returns (status, detail): ('ok', owner_name) when the claiming session is
    still the session behind that name; ('unclaimed', '') when nobody claimed
    the PR; ('gone', reason) otherwise.

    The identity check is the point. Relay names are reclaimable - re-register
    rebinds a name to a new tab, and `relay restore` depends on that - so
    routing on the name alone would deliver 'your PR has changes requested' to
    a tab that has never seen the branch. Comparing the iTerm session id
    recorded at claim time makes 'is this still the author?' answerable."""
    if pr is None or not (_get(pr, "owner", "") or ""):
        return ("unclaimed", "")
    owner = pr["owner"]
    if session is None:
        return ("gone", f"no session registered as '{owner}'")
    if _get(session, "closed_at", 0):
        return ("gone", f"session '{owner}' closed")
    if _get(session, "iterm_session_id", "") != _get(pr, "owner_session_id",
                                                     ""):
        return ("gone", f"'{owner}' was rebound to a different tab since the "
                        f"PR was claimed")
    return ("ok", owner)
```

`re` is already imported at the top of `swarm.py`; confirm before adding a duplicate import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 iterm/test_swarm.py`
Expected: PASS, ending in `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add iterm/swarm.py iterm/test_swarm.py
git commit -m "feat(swarm): PR ref parsing and route resolution, identity-checked"
```

---

### Task 3: `relay pr set|claim|list`

**Files:**
- Modify: `iterm/cli.py` (handlers after `cmd_msgs`, parser entries after the `task` subparser block, exit-code note in the module docstring)
- Modify: `skills/relay-cli-reference.md`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.upsert_pr`, `db.claim_pr`, `db.list_prs`, `db.PR_STATES`, `swarm.parse_pr_ref`, `swarm.resolve_pr_route`, `swarm.fmt_age`, `cli._err`, `cli._require_me`, `cli.my_iterm_id`.
- Produces: `cli.cmd_pr_set`, `cli.cmd_pr_claim`, `cli.cmd_pr_list`, `cli._pr_ref_or_err(ref) -> tuple[Optional[tuple[str,int]], int]`.

- [ ] **Step 1: Write the failing tests**

Append to the `run()` body of `iterm/test_cli.py`. The file already sets `RELAY_DB` and `ITERM_SESSION_ID` to temp values at import (lines 16-17) and provides `run_cli(*argv, iterm_id=None) -> (code, stdout, stderr)` (line 29) plus `check(msg, cond)` (line 24). Use those:

```python
    # --- relay pr set / claim / list ----------------------------------------
    cli.main(["register", "--name", "api-worker", "--role", "worker",
              "--project", "webshop"])

    rc, out, err = run_cli("pr", "set", "acme/api#482", "--state", "changes",
                         "--title", "Add rate limiting")
    ok &= check("pr set exits 0", rc == 0)
    ok &= check("pr set confirms the ref and state",
                "acme/api#482" in out and "changes" in out)

    rc, out, err = run_cli("pr", "set", "acme/api#482", "--state", "sideways")
    ok &= check("pr set rejects an unknown state with usage exit 2", rc == 2)

    rc, out, err = run_cli("pr", "set", "acme/api", "--state", "changes")
    ok &= check("pr set rejects a malformed ref", rc == 1)
    ok &= check("the malformed-ref error teaches the format",
                "owner/name#number" in err)

    rc, out, err = run_cli("pr", "claim", "acme/api#482", "--task", "14")
    ok &= check("pr claim exits 0", rc == 0)
    ok &= check("pr claim names the owner it recorded", "api-worker" in out)

    rc, out, err = run_cli("pr", "list")
    ok &= check("pr list shows the PR, its state and its owner",
                "acme/api#482" in out and "changes" in out
                and "api-worker" in out)
    ok &= check("pr list shows the age of the report next to the state",
                "ago" in out or "s " in out)

    run_cli("pr", "set", "acme/bff#77", "--state", "review")
    rc, out, err = run_cli("pr", "list")
    ok &= check("an unclaimed PR is listed and marked UNCLAIMED",
                "acme/bff#77" in out and "UNCLAIMED" in out)

    rc, out, err = run_cli("pr", "list", "--mine")
    ok &= check("--mine hides PRs this session did not claim",
                "acme/api#482" in out and "acme/bff#77" not in out)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse exits 2 with "invalid choice: 'pr'"

- [ ] **Step 3: Implement the handlers**

In `iterm/cli.py`, after `cmd_msgs`:

```python
def _pr_ref_or_err(ref: str):
    """(repo, number) or (None, exit_code). One format, taught on failure."""
    parsed = swarm.parse_pr_ref(ref)
    if parsed is None:
        return None, _err(f"'{ref}' is not a PR reference - use "
                          f"owner/name#number, e.g. acme/api#482")
    return parsed, 0


def cmd_pr_set(args) -> int:
    """The sweep session's verb: push what GitHub currently says. Relay never
    looks - it stores what it was told, and the UI always shows how old that
    telling is."""
    ref, rc = _pr_ref_or_err(args.ref)
    if ref is None:
        return rc
    repo, number = ref
    conn = db.connect()
    me = whoami(conn)
    project = args.project if args.project is not None else (
        me["project"] if me else "")
    row = db.upsert_pr(conn, repo, number, project=project, state=args.state,
                       title=args.title, branch=args.branch)
    print(f"{repo}#{number} -> {row['state']}"
          + (f" ({row['title']})" if row["title"] else ""))
    return 0


def cmd_pr_claim(args) -> int:
    """The worker's verb, run right after `gh pr create`. This is the only
    thing that makes 'who owns this PR' answerable later."""
    ref, rc = _pr_ref_or_err(args.ref)
    if ref is None:
        return rc
    repo, number = ref
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    row = db.claim_pr(conn, repo, number, owner=me["name"],
                      owner_session_id=sid, task_id=args.task,
                      branch=args.branch,
                      project=args.project if args.project is not None
                      else me["project"])
    task = f" for task #{row['task_id']}" if row["task_id"] else ""
    print(f"{repo}#{number} claimed by {me['name']}{task}")
    return 0


def cmd_pr_list(args) -> int:
    conn = db.connect()
    me = whoami(conn)
    if args.mine and me is None:
        return _err("--mine needs a registered session - run relay register")
    days = args.days if args.days is not None else _pr_retention_days()
    since = time.time() - float(days) * 86400
    rows = db.list_prs(conn, project=args.project,
                       owner=me["name"] if args.mine else None, since=since)
    if not rows:
        print("no pull requests")
        return 0
    sessions = {s["name"]: s for s in db.list_sessions(conn)}
    for r in rows:
        status, detail = swarm.resolve_pr_route(r, sessions.get(r["owner"]))
        who = (r["owner"] if status == "ok"
               else "UNCLAIMED" if status == "unclaimed"
               else f"{r['owner']} (GONE)")
        task = f"  #{r['task_id']}" if r["task_id"] else ""
        age = swarm.fmt_age(time.time() - r["state_changed_at"])
        print(f"{r['repo']}#{r['number']:<6} {r['state']:<9} {age:>4} ago  "
              f"{who}{task}")
    return 0
```

Add this helper next to `_PAYLOAD_WARN_LEN` near the top of `cli.py`:

```python
def _pr_retention_days() -> float:
    """Shared by `pr list` and the TUI's launch-time prune so the CLI window
    and the pane window cannot drift apart."""
    try:
        return float(os.environ.get("RELAY_PR_RETENTION_DAYS", "7"))
    except ValueError:
        return 7.0
```

- [ ] **Step 4: Add the parser entries**

In `build_parser()`, after the `task` subparser block:

```python
    pr = sub.add_parser("pr", help="pull requests: who owns which PR")
    prsub = pr.add_subparsers(dest="pr_verb", required=True)

    prs_ = prsub.add_parser("set", help="push a PR's current state "
                                        "(the sweep session's verb)")
    prs_.add_argument("ref", help="owner/name#number, e.g. acme/api#482")
    prs_.add_argument("--state", required=True, choices=db.PR_STATES)
    prs_.add_argument("--title", default=None)
    prs_.add_argument("--branch", default=None)
    prs_.add_argument("--project", default=None)
    prs_.set_defaults(fn=cmd_pr_set)

    prc = prsub.add_parser("claim", help="record that THIS session opened "
                                         "this PR")
    prc.add_argument("ref", help="owner/name#number, e.g. acme/api#482")
    prc.add_argument("--task", type=int, default=None)
    prc.add_argument("--branch", default=None)
    prc.add_argument("--project", default=None)
    prc.set_defaults(fn=cmd_pr_claim)

    prl = prsub.add_parser("list", help="PRs with state, age, owner")
    prl.add_argument("--project", default=None)
    prl.add_argument("--mine", action="store_true")
    prl.add_argument("--days", type=float, default=None,
                     help="window in days (default: RELAY_PR_RETENTION_DAYS)")
    prl.set_defaults(fn=cmd_pr_list)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_cli.py`
Expected: PASS

- [ ] **Step 6: Document the verbs**

In `skills/relay-cli-reference.md`, after the `relay task list` block, add:

```
    relay pr set <owner/name>#<n> --state created|review|changes|approved|merged|closed
                  [--title <t>] [--branch <b>] [--project <p>]
        Push a PR's CURRENT state into relay. Relay never calls gh and never
        looks at GitHub - it stores what you tell it, and everything that
        displays a state also displays how old that report is. Run this for
        every PR your sweep sees, claimed or not: an unclaimed PR that relay
        knows about shows up as UNCLAIMED instead of being invisible.

    relay pr claim <owner/name>#<n> [--task <id>] [--branch <b>]
        Record that THIS session opened this PR. Run it immediately after
        `gh pr create`, in the same breath as committing. This is the only
        thing that makes "which session did this PR" answerable later - a PR
        you do not claim can never be routed back to you automatically.

    relay pr list [--project <p>] [--mine] [--days <n>]
        PRs in stable order (repo, then number) with state, age of that state,
        owner, task, and an UNCLAIMED or GONE marker. --days defaults to
        RELAY_PR_RETENTION_DAYS (7).
```

- [ ] **Step 7: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py skills/relay-cli-reference.md
git commit -m "feat(cli): relay pr set|claim|list"
```

---

### Task 4: `relay send --pr` and `relay send --human`

**Files:**
- Modify: `iterm/cli.py` (`cmd_send`, `cmd_register`, the `send` parser, module docstring exit codes)
- Modify: `iterm/watcher.py` (`_check_escalations:888`)
- Modify: `iterm/swarm.py` (add `escalations_to_close` after `escalation_pings:81`)
- Modify: `skills/relay-cli-reference.md`
- Test: `iterm/test_cli.py`, `iterm/test_swarm.py`

**Interfaces:**
- Consumes: `swarm.parse_pr_ref`, `swarm.resolve_pr_route`, `db.get_pr`, `db.get_session`, `db.touch_pr_routed`, `db.queue_message`, `db.RESERVED_NAMES`, `db.mark_delivered`.
- Produces: `cli.EXIT_UNCLAIMED = 3`, `cli.EXIT_OWNER_GONE = 4`, `cli.HUMAN = "human"`.

- [ ] **Step 1: Write the failing tests**

Append to `iterm/test_cli.py`:

```python
    # --- relay send --pr ----------------------------------------------------
    cli.main(["register", "--name", "pr-sweep", "--role", "coordinator",
              "--project", "webshop"])

    rc, out, err = run_cli("send", "--pr", "acme/api#482",
                         "changes requested: tighten the rate limit test")
    ok &= check("routing to a live claiming session exits 0", rc == 0)
    ok &= check("success names the resolved owner", "api-worker" in out)

    rc, out, err = run_cli("send", "--pr", "acme/bff#77", "please fix")
    ok &= check("an unclaimed PR exits 3", rc == 3)
    ok &= check("the unclaimed error names the ref", "acme/bff#77" in err)

    rc, out, err = run_cli("send", "--pr", "acme/nope#1", "please fix")
    ok &= check("a PR relay never heard of also exits 3", rc == 3)

    # Rebind api-worker to a DIFFERENT tab, then route again.
    _rebind("api-worker", "SID-OTHER")
    rc, out, err = run_cli("send", "--pr", "acme/api#482", "please fix")
    ok &= check("a rebound owner exits 4, not 0", rc == 4)
    ok &= check("the owner-gone error explains why", "rebound" in err)

    rc, out, err = run_cli("send", "--pr", "acme/api", "please fix")
    ok &= check("a malformed ref is a plain user error (exit 1)", rc == 1)

    # --- relay send --human -------------------------------------------------
    rc, out, err = run_cli("send", "--human", "PR 77 is unclaimed - who owns it?")
    ok &= check("send --human exits 0", rc == 0)
    ok &= check("the human escalation is stored undelivered as an escalation",
                _one_message(to_name="human")["kind"] == "escalation"
                and _one_message(to_name="human")["delivered_at"] is None)

    rc, out, err = run_cli("register", "--name", "human", "--role", "worker")
    ok &= check("'human' cannot be registered as a session name", rc == 1)
    ok &= check("the reserved-name error explains what human is",
                "reserved" in err)

    rc, out, err = run_cli("send", "--human", "--pr", "acme/api#482", "both")
    ok &= check("two target forms at once is an error", rc != 0)
```

Add the two helpers this needs, next to the file's existing helpers:

```python
def _rebind(name, sid):
    """Simulate the name being reclaimed by a different tab."""
    c = db.connect()
    c.execute("UPDATE sessions SET iterm_session_id = ? WHERE name = ?",
              (sid, name))
    c.commit()
    c.close()


def _one_message(to_name):
    c = db.connect()
    row = c.execute("SELECT * FROM messages WHERE to_name = ? "
                    "ORDER BY id DESC LIMIT 1", (to_name,)).fetchone()
    c.close()
    return row
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - `--pr` is an unrecognized argument (argparse exit 2, not the expected 0/3/4)

- [ ] **Step 3: Implement routing and the human recipient**

Near the top of `iterm/cli.py`, after `_MAX_TIMERS_PER_SESSION`:

```python
# Exit codes beyond the usual 0/1/2, so a sweep session can branch on WHY a
# route failed instead of parsing stderr.
EXIT_UNCLAIMED = 3      # relay has no owner recorded for that PR
EXIT_OWNER_GONE = 4     # it had one, and that session is no longer there

# The operator's mailbox. Never a registered session, so nothing is ever
# injected into it; the watcher pings and marks it read.
HUMAN = "human"
```

Replace the body of `cmd_send` between the `--all` block and the plain-recipient block with the new target forms. The full ordering inside `cmd_send`, after the existing kind validation:

```python
    targets = sum(1 for f in (args.all, args.pr, args.human) if f)
    if targets > 1:
        return _err("pick one target: a name, --all, --pr, or --human")

    if args.human:
        body = args.to if args.body is None else args.body
        if not body:
            return _err('usage: relay send --human "<body>"')
        db.queue_message(conn, me["name"], HUMAN, body, me["project"],
                         kind="escalation")
        print("escalated to the human (sound + notification; not injected "
              "into any session)")
        return 0

    if args.pr:
        ref, rc = _pr_ref_or_err(args.pr)
        if ref is None:
            return rc
        repo, number = ref
        body = args.to if args.body is None else args.body
        if not body:
            return _err('usage: relay send --pr <owner/name>#<n> "<body>"')
        row = db.get_pr(conn, repo, number)
        owner_session = (db.get_session(conn, row["owner"])
                         if row is not None and row["owner"] else None)
        status, detail = swarm.resolve_pr_route(row, owner_session)
        if status == "unclaimed":
            print(f"relay: unclaimed: {repo}#{number} has no owner recorded "
                  f"- nobody ran `relay pr claim`. Escalate to the human.",
                  file=sys.stderr)
            return EXIT_UNCLAIMED
        if status == "gone":
            print(f"relay: owner gone: {detail}. Escalate to the human.",
                  file=sys.stderr)
            return EXIT_OWNER_GONE
        db.queue_message(conn, me["name"], detail, body, me["project"],
                         kind=kind)
        db.touch_pr_routed(conn, repo, number)
        task = f" (task #{row['task_id']})" if row["task_id"] else ""
        print(f"routed to {detail}{task}")
        return 0
```

The `--all` block stays exactly as it is, after this. Note that `args.to` doubles as the body when no recipient positional is given - the same trick `--all` already uses.

In `cmd_register`, replace the `relay`-only check:

```python
    if name in db.RESERVED_NAMES:
        return _err(f"'{name}' is reserved - 'relay' is the sender of system "
                    f"wake-ups and 'human' is the operator's escalation "
                    f"mailbox; pick another name")
```

In the `send` parser:

```python
    sd.add_argument("--pr", default=None, metavar="OWNER/NAME#N",
                    help="route to whichever session claimed this PR")
    sd.add_argument("--human", action="store_true",
                    help="escalate to the operator (pings; never injected)")
```

Update the module docstring's exit-code line to mention 3 and 4.

- [ ] **Step 4: Stop human escalations from queueing forever**

`prune_messages` only deletes *delivered* rows, and nothing delivers to `human`, so without this the operator's mailbox grows without bound and `_escalation_pinged` (in-memory) re-pings every stored escalation after each relay restart.

In `iterm/watcher.py`, inside `_check_escalations`, in the loop that adds to `self._escalation_pinged`:

```python
            for m in fresh:
                self._escalation_pinged.add(m["id"])
                self._note(f"ESCALATION from {m['from_name']} -> "
                           f"{m['to_name']}: {m['body'][:80]}")
                # 'human' is not a session: nothing will ever inject it, so
                # the ping IS the delivery. Marking it read keeps the operator
                # mailbox prunable and stops a restart re-ringing old news.
                # Dry-run never writes, matching the _dryrun_delivered rule.
                if m["to_name"] == "human" and not self.dry_run:
                    swarmdb.mark_delivered(self._swarm_conn(), m["id"])
```

The dry-run attribute is `self.dry_run` (defined at `watcher.py:241`).

The decision itself goes in `swarm.py` so it is testable without constructing a watcher, matching how every other delivery decision in this codebase is structured. Add after `escalation_pings`:

```python
def escalations_to_close(pinged) -> list:
    """Message ids that the ping itself has fully handled: those addressed to
    'human'. Nothing injects into the operator's mailbox, so an unmarked one
    would sit undelivered forever - never pruned (prune_messages keeps queued
    rows on purpose) and re-pinged on every relay restart, since the pinged
    set is in memory."""
    return [m["id"] for m in pinged if m["to_name"] == "human"]
```

and in the watcher loop use it:

```python
            for m in fresh:
                self._escalation_pinged.add(m["id"])
                self._note(f"ESCALATION from {m['from_name']} -> "
                           f"{m['to_name']}: {m['body'][:80]}")
            if not self.dry_run:
                for mid in swarm.escalations_to_close(fresh):
                    swarmdb.mark_delivered(self._swarm_conn(), mid)
```

Add to `iterm/test_swarm.py`:

```python
    fresh = [{"id": 1, "to_name": "human", "from_name": "pr-sweep"},
             {"id": 2, "to_name": "api-worker", "from_name": "pr-sweep"}]
    ok &= check("a human escalation is closed by the ping itself",
                swarm.escalations_to_close(fresh) == [1])
    ok &= check("a session-addressed escalation stays queued for injection",
                2 not in swarm.escalations_to_close(fresh))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_cli.py && python3 iterm/test_swarm.py`
Expected: both PASS

- [ ] **Step 6: Document the verbs**

In `skills/relay-cli-reference.md`, extend the `relay send` block:

```
    relay send --pr <owner/name>#<n> "<body>" [--kind <k>]
        Route a message to whichever session claimed that PR. Exit 0 and
        prints the owner it resolved to. Exit 3 = unclaimed (nobody ran
        `relay pr claim`). Exit 4 = the owner session is gone (closed, or its
        name was rebound to a different tab). Relay never guesses: it will not
        hand the PR to a different worker, because a session with no context
        on that branch produces a plausible fix that misses the point. On 3 or
        4, batch the misses and escalate once with --human.

    relay send --human "<body>"
        Escalate to the operator. Plays the sound, posts the notification, and
        shows in the swarm feed. It is NEVER injected into any session, so use
        it for the decisions only a human can make. Batch a sweep's misses into
        one message rather than firing one per PR.
```

- [ ] **Step 7: Commit**

```bash
git add iterm/cli.py iterm/watcher.py iterm/swarm.py iterm/test_cli.py iterm/test_swarm.py skills/relay-cli-reference.md
git commit -m "feat(cli): route PR feedback to its author, escalate to the human when it cannot"
```

---

### Task 5: PR pane in the swarm view

**Files:**
- Modify: `iterm/swarm.py` (`fleet_line`, `render_swarm`, new render helpers)
- Modify: `iterm/app.py` (`_render_swarm_view:1824`, launch-time prune near `prune_messages:956`)
- Test: `iterm/test_swarm.py`

**Interfaces:**
- Consumes: `swarm.resolve_pr_route`, `swarm.fmt_age`, `swarm._esc`, `swarm._clip`, `db.list_prs`, `db.prune_prs`.
- Produces: `swarm.pr_rows(prs, sessions, now) -> list[dict]` with keys `ref, state, age_s, owner_label, task_id, flag`; `swarm.render_prs(rows, width) -> list[str]`; `fleet_line(...)` gains a keyword-only `prs=()` argument; `render_swarm(...)` gains a keyword-only `prs=()` argument.

- [ ] **Step 1: Write the failing tests**

Append to `iterm/test_swarm.py`:

```python
    # --- PR pane ------------------------------------------------------------
    now = 10_000.0
    sess = [{"name": "api-worker", "iterm_session_id": "SID-A",
             "closed_at": 0, "role": "worker", "project": "webshop",
             "status_text": ""}]
    prs = [
        {"repo": "acme/api", "number": 482, "state": "changes",
         "state_changed_at": now - 4 * 3600, "owner": "api-worker",
         "owner_session_id": "SID-A", "task_id": 14, "project": "webshop"},
        {"repo": "acme/bff", "number": 77, "state": "changes",
         "state_changed_at": now - 86400, "owner": "",
         "owner_session_id": "", "task_id": None, "project": "webshop"},
        {"repo": "acme/api", "number": 480, "state": "merged",
         "state_changed_at": now - 2 * 86400, "owner": "api-worker",
         "owner_session_id": "SID-A", "task_id": 11, "project": "webshop"},
    ]
    rows = swarm.pr_rows(prs, sess, now)
    ok &= check("pr_rows preserves the stable repo/number order it was given",
                [r["ref"] for r in rows]
                == ["acme/api#482", "acme/bff#77", "acme/api#480"])
    ok &= check("changes-requested is flagged for attention",
                rows[0]["flag"] is True)
    ok &= check("an unclaimed PR is flagged and labelled UNCLAIMED",
                rows[1]["flag"] is True
                and rows[1]["owner_label"] == "UNCLAIMED")
    ok &= check("a merged PR is not flagged", rows[2]["flag"] is False)

    gone_rows = swarm.pr_rows(
        [dict(prs[0], owner_session_id="SID-OLD")], sess, now)
    ok &= check("an owner whose name was rebound is flagged GONE",
                gone_rows[0]["flag"] is True
                and "GONE" in gone_rows[0]["owner_label"])

    text = "\n".join(swarm.render_prs(rows, width=100))
    ok &= check("the pane shows the age beside every state, never a bare "
                "state", text.count("4h") >= 1 and text.count("1d") >= 1)
    ok &= check("flagged rows are duplicated into an attention strip above",
                text.count("acme/api#482") == 2)
    ok &= check("unflagged rows appear exactly once",
                text.count("acme/api#480") == 1)
    ok &= check("the attention strip sits above the separator",
                text.index("acme/api#482")
                < text.index("─") < text.rindex("acme/api#482"))

    ok &= check("render_prs is empty for no PRs", swarm.render_prs([], 100) == [])

    ok &= check("the fleet line counts PRs and how many need work",
                "PRs 3 · 2 need work"
                in swarm.fleet_line(sess, [], prs=rows))

    full = swarm.render_swarm(sess, [], [], now, width=100, prs=prs)
    ok &= check("render_swarm includes the PR pane", "PULL REQUESTS" in full)
    ok &= check("render_swarm still works with no prs argument at all",
                "PULL REQUESTS" not in swarm.render_swarm(sess, [], [], now,
                                                          width=100))

    kb = swarm.render_swarm(
        sess,
        [{"id": 14, "project": "webshop", "parent_id": None, "state": "doing",
          "title": "rate limiting", "owner": "api-worker"}],
        [], now, width=120, prs=prs)
    ok &= check("a task with a PR shows it on its kanban card",
                "PR 482" in kb)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_swarm.py`
Expected: FAIL with `AttributeError: module 'swarm' has no attribute 'pr_rows'`

- [ ] **Step 3: Implement the pane**

In `iterm/swarm.py`, after `interaction_rows`:

```python
# State glyphs share the vocabulary the rest of the swarm view already uses:
# ⊘ is the blocked/needs-work mark, ✓ is done, ◷ is waiting.
_PR_GLYPH = {"created": "◦", "review": "◷", "changes": "⊘",
             "approved": "✓", "merged": "●", "closed": "✕"}
_PR_COLOR = {"changes": "yellow", "approved": "green", "merged": "dim",
             "closed": "dim"}

# States that mean a human or a worker still owes this PR something.
_PR_NEEDS_WORK = ("changes",)


def pr_rows(prs, sessions, now: float) -> list:
    """One display row per PR, in the order given (list_prs already returns
    the stable repo/number order). `flag` marks the rows that need attention:
    changes requested, nobody claimed it, or the claiming session is gone."""
    by_name = {s["name"]: s for s in sessions}
    out = []
    for p in prs:
        status, _ = resolve_pr_route(p, by_name.get(_get(p, "owner", "")))
        if status == "ok":
            label, gone = p["owner"], False
        elif status == "unclaimed":
            label, gone = "UNCLAIMED", True
        else:
            label, gone = f"{p['owner']} GONE", True
        state = _get(p, "state", "created")
        out.append({
            "ref": f"{p['repo']}#{p['number']}",
            "state": state,
            "age_s": max(0.0, now - float(_get(p, "state_changed_at", 0) or 0)),
            "owner_label": label,
            "task_id": _get(p, "task_id"),
            "flag": bool(gone or state in _PR_NEEDS_WORK),
        })
    return out


def _pr_line(r, width: int, mark: str = " ") -> str:
    """Age always rides beside the state: relay only knows what a session last
    told it, and a bare 'approved' would read as fact. `mark` is how the same
    row renders in the attention strip, so the strip and the list cannot drift
    apart in formatting."""
    glyph = _PR_GLYPH.get(r["state"], "·")
    task = f"  #{r['task_id']}" if r["task_id"] else ""
    line = (f" {mark} {_clip(r['ref'], 22):<22} {glyph}{r['state']:<9} "
            f"{fmt_age(r['age_s']):>4}  {_clip(r['owner_label'], 18)}{task}")
    color = ("red" if "UNCLAIMED" in r["owner_label"]
             or "GONE" in r["owner_label"]
             else _PR_COLOR.get(r["state"]))
    return f"[{color}]{_esc(line)}[/{color}]" if color else _esc(line)


def render_prs(rows, width: int = 100) -> list:
    """Attention strip on top, then every PR in stable order below it. The
    main list never reorders as states change, so a row stays where the eye
    last found it; anything urgent is DUPLICATED above rather than moved."""
    if not rows:
        return []
    out = ["PULL REQUESTS"]
    flagged = [r for r in rows if r["flag"]]
    for r in flagged:
        out.append(_pr_line(r, width, mark="‼"))
    if flagged:
        out.append("  " + "─" * max(10, min(width - 4, 60)))
    for r in rows:
        out.append(_pr_line(r, width))
    return out
```

In `fleet_line`, add the keyword argument and the segment:

```python
def fleet_line(sessions, tasks, stale=frozenset(), queued: int = 0,
               prs=()) -> str:
    ...
    if queued:
        bits.append(f"msgs {queued} queued")
    if prs:
        need = sum(1 for r in prs if r["flag"])
        bits.append(f"PRs {len(prs)}" + (f" · {need} need work" if need else ""))
    return "FLEET  " + " · ".join(bits)
```

In `render_swarm`, add `prs=()` as a keyword argument, compute rows once, pass them to `fleet_line`, append the pane before the `MESSAGES` block, and add the kanban suffix.

```python
def render_swarm(sessions, tasks, messages, now: float, width: int = 100,
                 stale=frozenset(), activity=None, prs=()) -> str:
    ...
    prows = pr_rows(prs, sessions, now) if prs else []
    out.append(_esc(fleet_line(sessions, tasks, stale=stale, queued=queued,
                               prs=prows)))
```

For the kanban card suffix, build a lookup before the column comprehension and extend the cell text:

```python
        # A task that produced a PR carries it on its card - the kanban stays
        # relay's own state machine, the PR pane stays the authority on PR
        # state, and this is the one thread between them.
        pr_by_task = {p["task_id"]: p for p in prs
                      if _get(p, "task_id") is not None}
        def _card(t):
            p = pr_by_task.get(t["id"])
            suffix = (f" ▸ PR {p['number']} {_PR_GLYPH.get(p['state'], '')}"
                      f"{p['state']}" if p else "")
            head = f"#{t['id']} "
            return head + _clip(t["title"], max(4, colw - len(head)
                                                - len(suffix))) + suffix
        cols = {st: [_card(t) for t in p_tasks if t["state"] == st]
                for st in _STATE_COLS}
```

Then, immediately before `out.append("MESSAGES")`:

```python
    pane = render_prs(prows, width)
    if pane:
        out.extend(pane)
        out.append("")
```

- [ ] **Step 4: Wire it into the app**

In `iterm/app.py`, in `_render_swarm_view` (line 1824), load the PRs and pass them:

```python
            prs = [dict(r) for r in swarmdb.list_prs(
                self._swarm_db,
                since=_time.time() - _pr_retention_days() * 86400)]
            ...
            text = swarmlogic.render_swarm(sessions, tasks, msgs,
                                           _time.time(), width=w,
                                           stale=stale, activity=activity,
                                           prs=prs)
```

Add the module-level helper next to the other env reads in `app.py` (do not import it from `cli.py`; `app.py` does not import `cli`):

```python
def _pr_retention_days() -> float:
    try:
        return float(os.environ.get("RELAY_PR_RETENTION_DAYS", "7"))
    except ValueError:
        return 7.0
```

In the launch-time prune block (near line 956), add the PR prune inside the same `try`:

```python
            swarmdb.prune_messages(
                _mc, float(os.environ.get("RELAY_MSG_RETENTION_DAYS", "7")))
            swarmdb.prune_prs(_mc, _pr_retention_days())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_swarm.py && python3 iterm/test_app.py`
Expected: both PASS. `test_app.py` must still pass unchanged - `render_swarm`'s new argument is keyword-only with a default, so every existing call site keeps working.

- [ ] **Step 6: Commit**

```bash
git add iterm/swarm.py iterm/app.py iterm/test_swarm.py
git commit -m "feat(tui): PR pane in the swarm view - attention strip over a stable list"
```

---

### Task 6: `relay doctor` PR block and README

**Files:**
- Modify: `iterm/cli.py` (`cmd_doctor:589`)
- Modify: `README.md`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.list_prs`, `swarm.pr_rows`.
- Produces: nothing new; extends existing output.

- [ ] **Step 1: Write the failing test**

Append to `iterm/test_cli.py`:

```python
    rc, out, err = run_cli("doctor")
    ok &= check("doctor exits 0", rc == 0)
    ok &= check("doctor reports PR counts", "PULL REQUESTS" in out)
    ok &= check("doctor surfaces PRs that cannot be routed",
                "unclaimed" in out.lower() or "UNCLAIMED" in out)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - "PULL REQUESTS" not in doctor output

- [ ] **Step 3: Extend `cmd_doctor`**

Add before `cmd_doctor`'s final `return 0`:

```python
    prs = [dict(r) for r in db.list_prs(conn)]
    if prs:
        rows = swarm.pr_rows(prs, [dict(s) for s in db.list_sessions(conn)],
                             time.time())
        by_state = {}
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        print()
        print("PULL REQUESTS  " + " · ".join(
            f"{k} {v}" for k, v in sorted(by_state.items())))
        for r in rows:
            if r["flag"]:
                print(f"  ‼ {r['ref']:<22} {r['state']:<9} "
                      f"{r['owner_label']}")
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python3 iterm/test_cli.py`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`

- [ ] **Step 6: Document it in the README**

Add a subsection under the swarm material:

```markdown
### Pull requests: who owns what

Relay never calls `gh`. A session tells it what it sees, and relay answers one
question in return: **which session opened this PR?**

A worker claims its PR the moment it opens one:

    relay pr claim acme/api#482 --task 14

A PR-sweep session pushes what GitHub currently says, then routes feedback
straight to whoever wrote the code:

    relay pr set acme/api#482 --state changes
    relay send --pr acme/api#482 "changes requested: tighten the rate limit test"

That message is typed into the claiming session when it goes idle. If nobody
claimed the PR (exit 3), or the claiming session is closed or its name has been
rebound to a different tab (exit 4), relay refuses to guess and you decide:

    relay send --human "acme/bff#77 has changes requested and no owner"

which pings you immediately and is never injected into any session. `TAB` shows
the PR pane: what needs work on top, then every PR in stable order, each with
the age of the last report beside its state - relay only knows what it was
last told, and the pane never pretends otherwise.

Retention is `RELAY_PR_RETENTION_DAYS` (default 7). Merged and closed PRs age
out; open ones never do.
```

- [ ] **Step 7: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py README.md
git commit -m "feat(cli): doctor reports PR health; README covers PR routing"
```

---

## Verification

Before calling this plan done:

- [ ] `./test/run.sh` prints `ALL SUITES PASSED`
- [ ] `grep -rnP '\x{2014}' iterm/ README.md skills/ docs/plans/` returns nothing
- [ ] `grep -rn 'gh \|github.com' iterm/` returns nothing new from this work
- [ ] An existing `~/.relay/relay.db` opens without migration error and `PRAGMA user_version` still reports 8
