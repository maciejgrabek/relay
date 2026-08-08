"""Relay swarm state - the SQLite layer. The DB is the bus.

All swarm state (registered sessions, queued messages, tasks) lives in ONE
SQLite file, default ~/.relay/relay.db (override: RELAY_DB). CLI verbs run by
Claude sessions write rows and exit; the watcher polls and delivers. WAL mode
+ busy_timeout so many short-lived writers coexist; there is no daemon.

No iTerm2 imports here on purpose - this module is pure stdlib and is
unit-tested against temp DB files (test_db.py).
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import List, Optional

ROLES = ("worker", "coordinator")
TASK_STATES = ("todo", "doing", "blocked", "done")
# Arm levels a spawner may request for a new worker (applied by the watcher
# when it first sees the session; "off" is expressed by no request at all).
ARM_REQUEST_MODES = ("safe", "wild", "insane")

# Message kinds with dedicated rendering/behavior. 'wake' is reserved for
# relay-generated wake-ups; custom kinds beyond this set are allowed and
# render plain. Validation lives in the CLI - the DB stores what it is given.
MESSAGE_KINDS = ("info", "done", "blocked", "escalation", "wake",
                 "say", "agree", "ask")

# A discussion is open until it reaches a verdict. 'unresolved' is a NORMAL
# outcome, not a failure: sessions that cannot converge is information the
# operator wants, and the alternative is looping forever or fabricating an
# agreement.
# 'agreed' is the only state relay reaches on its own, and only by reading that
# every participant posted `agree`. 'closed' is an ending the AGENTS declared
# via `relay close`. Relay never judges a discussion failed - deciding that,
# and deciding what happens next, belongs to the sessions having it.
THREAD_STATES = ("open", "agreed", "closed")

PR_STATES = ("created", "review", "changes", "approved", "merged", "closed")

# Names no session may register. 'relay' is the sender of system wake-ups;
# 'human' is the recipient of operator escalations, which must never resolve
# to a tab that could be injected into.
RESERVED_NAMES = ("relay", "human")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  name TEXT PRIMARY KEY,
  iterm_session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  status_text TEXT NOT NULL DEFAULT '',
  registered_at REAL NOT NULL,
  last_seen REAL NOT NULL,
  arm_request TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT '',
  workdir TEXT NOT NULL DEFAULT '',
  spawn_prompt TEXT NOT NULL DEFAULT '',
  closed_at REAL NOT NULL DEFAULT 0,
  worktree_repo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  from_name TEXT NOT NULL,
  to_name TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at REAL NOT NULL,
  delivered_at REAL,
  kind TEXT NOT NULL DEFAULT 'info',
  reply_to INTEGER,
  thread_id INTEGER
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  parent_id INTEGER,
  title TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'todo',
  owner TEXT,
  spec_path TEXT,
  blocked_by TEXT NOT NULL DEFAULT '',
  created_by TEXT,
  updated_at REAL NOT NULL,
  parked INTEGER NOT NULL DEFAULT 0,
  workdir TEXT NOT NULL DEFAULT '',
  context TEXT NOT NULL DEFAULT ''
);
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
  max_fires INTEGER NOT NULL DEFAULT 10,
  fire_count INTEGER NOT NULL DEFAULT 0,
  key TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0
);
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
"""

# Indexes live OUTSIDE _SCHEMA and run AFTER _migrate, never inside it.
# _SCHEMA is `CREATE TABLE IF NOT EXISTS` only, so it is a no-op on an existing
# DB - but an index over a column that a migration adds (timers.key, migration
# 6) would NOT be a no-op there: on a pre-migration DB it references a column
# that does not exist yet and raises OperationalError, aborting executescript
# and silently skipping every statement after it. Keeping indexes here means
# _SCHEMA stays loud about real failures, new tables can keep being appended to
# _SCHEMA with no migration (the established idiom - see _MIGRATIONS[5]), and
# both fresh and migrated DBs get the indexes.
_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS timers_sid_key ON timers(iterm_session_id, key)
  WHERE key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_prs_ref ON prs(repo, number);
"""


def default_path() -> str:
    """Read RELAY_DB at call time (not import time) so tests can override."""
    return os.path.expanduser(os.environ.get("RELAY_DB", "~/.relay/relay.db"))


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or default_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(_SCHEMA)     # tables only, and deliberately not caught:
                                    # a failure here is a real one (locked DB,
                                    # I/O error, a typo in _SCHEMA).
    # Schema versioning: 0 = fresh (CREATEs above built the current schema),
    # otherwise migrate step by step. v2 added sessions.arm_request, v3 added
    # sessions.mode (persisted arm level, so a relay restart doesn't disarm a
    # live swarm), v4 added sessions.workdir/spawn_prompt/closed_at (restore
    # context for a dead session, and whether it's closed), v5 added
    # messages.kind and sessions.worktree_repo.
    _migrate(conn)                  # guarantees timers.key exists
    conn.executescript(_INDEXES)    # fresh and migrated DBs both get it
    return conn


_CURRENT_VERSION = 11
_MIGRATIONS = {
    # from_version: (SQL to run, ...)
    1: ("ALTER TABLE sessions ADD COLUMN arm_request TEXT NOT NULL DEFAULT ''",),
    2: ("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT ''",),
    3: ("ALTER TABLE sessions ADD COLUMN workdir TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN spawn_prompt TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN closed_at REAL NOT NULL DEFAULT 0"),
    4: ("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'info'",
        "ALTER TABLE sessions ADD COLUMN worktree_repo TEXT NOT NULL DEFAULT ''"),
    # v6: per-timer fire cap. The timers table (added via _SCHEMA without a
    # version bump) predates these columns on any DB that already ran the
    # timers feature, so existing DBs need the ALTERs; fresh DBs get them from
    # _SCHEMA. "column already present" is swallowed by _migrate, so a DB that
    # got the columns from _SCHEMA before this bump migrates harmlessly.
    5: ("ALTER TABLE timers ADD COLUMN max_fires INTEGER NOT NULL DEFAULT 10",
        "ALTER TABLE timers ADD COLUMN fire_count INTEGER NOT NULL DEFAULT 0"),
    # v7: self-scheduling. CLI-created timers carry a stable per-session `key`
    # so a session re-registering the same responsibility upserts instead of
    # stacking duplicates. Overlay-created rows keep key='' and are never
    # matched by get_timer_by_key.
    6: ("ALTER TABLE timers ADD COLUMN key TEXT NOT NULL DEFAULT ''",),
    # v8: partial unique index on (iterm_session_id, key), so the upsert-by-
    # key invariant the CLI relies on (session self-scheduling design §7) is
    # a DB-level guarantee, not just a CLI-level convention. Partial (`WHERE
    # key != ''`) because overlay-created rows all carry key='' and must not
    # collide with each other.
    # The DELETE runs FIRST and is not optional: cmd_timer_add's lookup-then-
    # insert is not atomic, so a DB written while v7 was current can already
    # hold duplicate (iterm_session_id, key) rows. CREATE UNIQUE INDEX over
    # those raises IntegrityError, which _migrate does NOT swallow - that
    # would make every db.connect() (TUI, watcher, every CLI verb) raise with
    # no way out. Keep the lowest id per group, drop the rest.
    7: ("DELETE FROM timers WHERE key != '' AND id NOT IN ("
        "SELECT MIN(id) FROM timers WHERE key != '' "
        "GROUP BY iterm_session_id, key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS timers_sid_key ON timers"
        "(iterm_session_id, key) WHERE key != ''"),
    # v9: RESERVED_NAMES ('relay', 'human') was only ever enforced by
    # db.register - a DB populated before that guard existed (or before
    # 'human' was reserved at all) can hold a real, deliverable `sessions`
    # row named 'human'. That row is exactly the operator's escalation
    # mailbox name, so `relay send --human` would queue a message that a
    # live registered tab then receives as typed input - the one thing the
    # escalation channel promises never to do. DELETE, not rename: the goal
    # is simply "no session may ever be bound to a reserved name" and a
    # plain delete is the smallest change that guarantees it. This is
    # deliberately NOT reset_owner_tasks/delete_tasks_for_owner - the tasks
    # table has no foreign key on sessions.name, so removing the sessions
    # row does not touch a single task row; any task this session owned
    # keeps owner='human' exactly as it was, still visible via `relay task
    # list` and reassignable by `relay clean`/`relay task update`. A no-op
    # DELETE (no such row) is the common case and costs nothing.
    8: ("DELETE FROM sessions WHERE name = 'human'",),
    # v10: session conversations. `reply_to` correlates an answer with the
    # message it answers, so `relay ask` cannot mistake unrelated traffic
    # arriving mid-wait for its reply; `thread_id` marks a message as a post in
    # a discussion. Both NULL on ordinary `relay send` traffic, which is why
    # every existing message path keeps working untouched.
    9: ("ALTER TABLE messages ADD COLUMN reply_to INTEGER",
        "ALTER TABLE messages ADD COLUMN thread_id INTEGER"),
    # v11: parked work. A parked row is a thought the operator captured
    # without spending a session's context - not assignable work, so every
    # coordinator-facing read filters parked=0. `workdir` is the address
    # (project is a swarm-routing label and two sessions of one project
    # routinely sit in sibling worktrees); `context` is an inert JSON stamp
    # of what the session was doing at capture time.
    10: ("ALTER TABLE tasks ADD COLUMN parked INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE tasks ADD COLUMN workdir TEXT NOT NULL DEFAULT ''",
         "ALTER TABLE tasks ADD COLUMN context TEXT NOT NULL DEFAULT ''"),
}


def _migrate(conn) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v == 0:
        # Fresh DB: _SCHEMA already built the current shape.
        conn.execute(f"PRAGMA user_version = {_CURRENT_VERSION}")
        return
    while v < _CURRENT_VERSION:
        for stmt in _MIGRATIONS.get(v, ()):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # The ONLY tolerated case: re-applying an ALTER TABLE ... ADD
                # COLUMN that already landed (a fresh DB got the column from
                # _SCHEMA, or an earlier migration was interrupted after
                # writing the column but before bumping user_version).
                # Anything else - a locked DB (relay runs a TUI, a watcher and
                # CLI invocations against one file, with only a 3s busy
                # timeout), a full disk, an I/O error - must propagate so this
                # step is NOT counted as done: re-raising here skips the
                # `v += 1` / PRAGMA user_version bump below, so the DB is left
                # at the last known-good version and the next connect() retries
                # this step instead of limping forward with a version stamped
                # ahead of the columns that actually exist.
                if "duplicate column" not in str(e).lower():
                    raise
        v += 1
        conn.execute(f"PRAGMA user_version = {v}")
    # Migration 7's dedupe is DML, which opens an implicit transaction; commit
    # so it cannot be rolled back by a connection that closes without writing.
    conn.commit()


def _now(now: Optional[float]) -> float:
    return now if now is not None else time.time()


# --- sessions ----------------------------------------------------------------

def register(conn, name: str, iterm_session_id: str, role: str,
             project: str = "", now: Optional[float] = None) -> None:
    """Insert or rebind a named session. Re-registering an existing name
    updates the binding (a respawned worker reclaims its identity) but keeps
    the original registered_at - and keeps the existing project when the
    re-register omits one (a spawned worker re-registering per the skill
    without --project must not wipe its pre-registered project). Also clears
    closed_at: a re-register revives a session that was previously marked
    closed."""
    if name in RESERVED_NAMES:
        raise ValueError(
            f"'{name}' is reserved - 'relay' is the sender of system "
            f"wake-ups and 'human' is the operator's escalation mailbox; "
            f"pick another name")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    t = _now(now)
    conn.execute(
        """INSERT INTO sessions(name, iterm_session_id, role, project,
                                status_text, registered_at, last_seen)
           VALUES(?,?,?,?,'',?,?)
           ON CONFLICT(name) DO UPDATE SET
             iterm_session_id=excluded.iterm_session_id,
             role=excluded.role,
             project=CASE WHEN excluded.project = ''
                          THEN sessions.project
                          ELSE excluded.project END,
             last_seen=excluded.last_seen,
             closed_at=0""",
        (name, iterm_session_id, role, project, t, t))
    conn.commit()


def get_session(conn, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sessions WHERE name=?", (name,)).fetchone()


def rename_session(conn, old: str, new: str) -> bool:
    """Rebind a session to a new name, carrying its mail and tasks with it.

    Auto-derived names exist to be replaced, so a rename must not orphan the
    session's history: messages addressed to the old name would otherwise sit
    undelivered forever, and its tasks would show an owner nobody can find.
    One transaction - a half-applied rename is worse than none."""
    if new in RESERVED_NAMES:
        raise ValueError(f"'{new}' is reserved")
    if old == new:
        return False
    with conn:
        cur = conn.execute("UPDATE sessions SET name=? WHERE name=?",
                           (new, old))
        if cur.rowcount == 0:
            return False
        conn.execute("UPDATE messages SET from_name=? WHERE from_name=?",
                     (new, old))
        conn.execute("UPDATE messages SET to_name=? WHERE to_name=?",
                     (new, old))
        conn.execute("UPDATE tasks SET owner=? WHERE owner=?", (new, old))
        conn.execute("UPDATE tasks SET created_by=? WHERE created_by=?",
                     (new, old))
        conn.execute("UPDATE prs SET owner=? WHERE owner=?", (new, old))
    return True


def registered_names(conn) -> set:
    """Every name currently bound to a non-closed session. Distinct from
    swarm.live_names, which is watcher-side and additionally requires the tab
    to be present right now; a name here is taken even if its tab is gone."""
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sessions WHERE closed_at = 0").fetchall()}


def get_by_iterm_id(conn, iterm_session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sessions WHERE iterm_session_id=?",
                        (iterm_session_id,)).fetchone()


def set_status(conn, name: str, status_text: str,
               now: Optional[float] = None) -> bool:
    cur = conn.execute(
        "UPDATE sessions SET status_text=?, last_seen=? WHERE name=?",
        (status_text, _now(now), name))
    conn.commit()
    return cur.rowcount > 0


def set_arm_request(conn, name: str, mode: str) -> bool:
    """Ask the watcher to arm this session at `mode` when it next sees it.
    Used by spawn so a new worker starts pre-armed. Local-trust caveat: any
    process that can write this DB can request arming - same boundary as
    queue_message, documented in the README's security posture."""
    if mode not in ARM_REQUEST_MODES:
        raise ValueError(f"mode must be one of {ARM_REQUEST_MODES}, got {mode!r}")
    cur = conn.execute("UPDATE sessions SET arm_request=? WHERE name=?",
                       (mode, name))
    conn.commit()
    return cur.rowcount > 0


def clear_arm_request(conn, name: str) -> None:
    conn.execute("UPDATE sessions SET arm_request='' WHERE name=?", (name,))
    conn.commit()


def set_session_mode(conn, name: str, mode: str) -> bool:
    """Persist a registered session's current arm level so a relay restart can
    restore it (the running arm state otherwise lives only in the TUI process).
    Written by the watcher when the human changes a mode; read only at first
    sight after a restart. Not an escalation channel: it takes effect only on
    the next restart, and direct DB writes are blocked in safe mode by
    lib/danger.sh (see [[arm-self-escalation-guard]] in the README)."""
    cur = conn.execute("UPDATE sessions SET mode=? WHERE name=?", (mode, name))
    conn.commit()
    return cur.rowcount > 0


def set_session_context(conn, name: str, workdir: str,
                        spawn_prompt: str) -> bool:
    """Persist where a session was spawned and its original mission, so a dead
    session can be restored in the right place with context."""
    cur = conn.execute(
        "UPDATE sessions SET workdir=?, spawn_prompt=? WHERE name=?",
        (workdir, spawn_prompt, name))
    conn.commit()
    return cur.rowcount > 0


def set_worktree_repo(conn, name: str, repo: str) -> bool:
    """Record that this session's workdir is a relay-created git worktree of
    `repo`, so wipe can offer to remove it (only when clean)."""
    cur = conn.execute("UPDATE sessions SET worktree_repo=? WHERE name=?",
                       (repo, name))
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


def list_sessions(conn, project: Optional[str] = None) -> List[sqlite3.Row]:
    if project is None:
        return conn.execute(
            "SELECT * FROM sessions ORDER BY registered_at").fetchall()
    return conn.execute(
        "SELECT * FROM sessions WHERE project=? ORDER BY registered_at",
        (project,)).fetchall()


# --- messages ------------------------------------------------------------------

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


def undelivered(conn, to_name: Optional[str] = None) -> List[sqlite3.Row]:
    if to_name is None:
        return conn.execute(
            "SELECT * FROM messages WHERE delivered_at IS NULL "
            "ORDER BY created_at, id").fetchall()
    return conn.execute(
        "SELECT * FROM messages WHERE delivered_at IS NULL AND to_name=? "
        "ORDER BY created_at, id", (to_name,)).fetchall()


def mark_delivered(conn, msg_id: int, now: Optional[float] = None) -> None:
    conn.execute("UPDATE messages SET delivered_at=? WHERE id=?",
                 (_now(now), msg_id))
    conn.commit()


def get_message(conn, msg_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM messages WHERE id=?",
                        (msg_id,)).fetchone()


def last_batch(conn, to_name: str) -> List[sqlite3.Row]:
    """The messages delivered to `to_name` in its most recent delivery.

    The watcher stamps ONE delivered_at across a whole batch, so a shared
    timestamp is the batch. Used by `relay reply` with no id: replying to "the
    last message" is only unambiguous when the last delivery held exactly one,
    and a silently mis-threaded reply is worse than one more argument."""
    row = conn.execute(
        "SELECT MAX(delivered_at) AS t FROM messages "
        "WHERE to_name=? AND delivered_at IS NOT NULL", (to_name,)).fetchone()
    if row is None or row["t"] is None:
        return []
    return conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND delivered_at=? "
        "ORDER BY id", (to_name, row["t"])).fetchall()


def find_reply(conn, to_name: str, ask_id: int, peer: str,
               since: float) -> Optional[sqlite3.Row]:
    """The answer to `ask_id`, correlated, with a forgiving fallback.

    Strict correlation first: `reply_to` is what stops unrelated traffic
    arriving mid-wait from being read as the answer. But a peer that answers
    with a plain `relay send` instead of `relay reply` has still answered, and
    hanging until timeout because it used the wrong verb would make the feature
    look broken when it was merely informal - so fall back to any message from
    that peer sent after the question."""
    row = conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND reply_to=? "
        "ORDER BY id LIMIT 1", (to_name, ask_id)).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM messages WHERE to_name=? AND from_name=? "
        "AND created_at > ? ORDER BY id LIMIT 1",
        (to_name, peer, since)).fetchone()


def message_history(conn, with_name: Optional[str] = None,
                    project: Optional[str] = None,
                    limit: int = 200) -> List[sqlite3.Row]:
    """Newest LAST (chronological), capped at `limit` most recent."""
    where, args = [], []
    if with_name is not None:
        where.append("(from_name=? OR to_name=?)")
        args += [with_name, with_name]
    if project is not None:
        where.append("project=?")
        args.append(project)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM messages {w} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*args, limit)).fetchall()
    return list(reversed(rows))


# --- threads (discussions) -------------------------------------------------------
#
# The table holds only what cannot be derived: topic, participants, cap and the
# recorded verdict. WHO HAS AGREED is deliberately NOT stored - it is computed
# from the messages (see swarm.positions), so there is no second copy of the
# truth to fall out of sync, and "a post retracts your agreement" needs no
# bookkeeping at all.

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
        (project, topic, opener, ",".join(names), int(rounds_cap), _now(now)))
    conn.commit()
    return cur.lastrowid


def get_thread(conn, tid: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()


def list_threads(conn, project: Optional[str] = None,
                 state: Optional[str] = None) -> List[sqlite3.Row]:
    where, args = [], []
    if project is not None:
        where.append("project=?")
        args.append(project)
    if state is not None:
        where.append("state=?")
        args.append(state)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT * FROM threads {w} ORDER BY id", tuple(args)).fetchall()


def thread_messages(conn, tid: int) -> List[sqlite3.Row]:
    """Every post in the thread, oldest first, deduped.

    A post to three participants is three message rows carrying the same body
    (one per recipient, so delivery/batching/inbox all keep working unchanged).
    The transcript must show it once."""
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
    """Close an OPEN thread. Guarded on state inside the UPDATE so two watcher
    ticks racing cannot double-close and ping the operator twice."""
    if state not in THREAD_STATES or state == "open":
        raise ValueError(f"state must be a terminal one of {THREAD_STATES}, "
                         f"got {state!r}")
    cur = conn.execute(
        "UPDATE threads SET state=?, outcome=?, closed_at=? "
        "WHERE id=? AND state='open'",
        (state, outcome, _now(now), tid))
    conn.commit()
    return cur.rowcount > 0


# --- tasks -----------------------------------------------------------------------

def add_task(conn, title: str, project: str = "", parent_id: Optional[int] = None,
             owner: Optional[str] = None, spec_path: Optional[str] = None,
             blocked_by=(), created_by: Optional[str] = None,
             now: Optional[float] = None) -> int:
    bb = ",".join(str(int(b)) for b in blocked_by)
    cur = conn.execute(
        """INSERT INTO tasks(project, parent_id, title, state, owner, spec_path,
                             blocked_by, created_by, updated_at)
           VALUES(?,?,?,'todo',?,?,?,?,?)""",
        (project, parent_id, title, owner, spec_path, bb, created_by, _now(now)))
    conn.commit()
    return cur.lastrowid


def get_task(conn, task_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def set_task_state(conn, task_id: int, state: str,
                   now: Optional[float] = None) -> bool:
    if state not in TASK_STATES:
        raise ValueError(f"state must be one of {TASK_STATES}, got {state!r}")
    cur = conn.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                       (state, _now(now), task_id))
    conn.commit()
    return cur.rowcount > 0


def list_tasks(conn, project: Optional[str] = None,
               owner: Optional[str] = None) -> List[sqlite3.Row]:
    where, args = [], []
    if project is not None:
        where.append("project=?")
        args.append(project)
    if owner is not None:
        where.append("owner=?")
        args.append(owner)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(f"SELECT * FROM tasks {w} ORDER BY id", args).fetchall()


def current_task_for(conn, owner: str) -> Optional[sqlite3.Row]:
    """The task to show in the TUI's TASK NOW column: an in-flight task if any
    (doing beats blocked beats todo), most recently updated first."""
    return conn.execute(
        """SELECT * FROM tasks WHERE owner=? AND state!='done'
           ORDER BY CASE state WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1
                    ELSE 2 END, updated_at DESC LIMIT 1""",
        (owner,)).fetchone()


# --- session timers ----------------------------------------------------------

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


def list_timers(conn, iterm_session_id) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM timers WHERE iterm_session_id=? ORDER BY id",
        (iterm_session_id,)).fetchall()


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


def all_timers(conn) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM timers ORDER BY id").fetchall()


def update_timer(conn, timer_id, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE timers SET {cols} WHERE id=?",
                 (*fields.values(), timer_id))
    conn.commit()


def set_timer_enabled(conn, timer_id, enabled, now: Optional[float] = None) -> None:
    """Toggle a timer on/off. Turning it ON resets the clock (last_fired_at=now)
    so it starts from a FULL interval, not from wherever the countdown was when
    it was switched off - toggling off/on restarts the cycle."""
    if enabled:
        conn.execute("UPDATE timers SET enabled=1, last_fired_at=? WHERE id=?",
                     (_now(now), timer_id))
    else:
        conn.execute("UPDATE timers SET enabled=0 WHERE id=?", (timer_id,))
    conn.commit()


def delete_timer(conn, timer_id) -> None:
    conn.execute("DELETE FROM timers WHERE id=?", (timer_id,))
    conn.commit()


def mark_timer_fired(conn, timer_id, now: Optional[float] = None) -> None:
    """Record a REAL fire: advance the clock AND consume one of the fire cap.
    The 'fire now' key and the dry-run path set last_fired_at via update_timer
    instead, so they do not consume the cap."""
    conn.execute(
        "UPDATE timers SET last_fired_at=?, fire_count=fire_count+1 WHERE id=?",
        (_now(now), timer_id))
    conn.commit()


def restore_timer(conn, timer_id, now: Optional[float] = None) -> int:
    """Re-activate ONE timer (active=1) and re-bind its clock/binding to now -
    the per-row restore the `r` key uses. Keeps fire_count (progress toward the
    cap is preserved)."""
    cur = conn.execute(
        "UPDATE timers SET active=1, last_fired_at=?, bound_at=? WHERE id=?",
        (_now(now), _now(now), timer_id))
    conn.commit()
    return cur.rowcount


def restart_timer(conn, timer_id, now: Optional[float] = None) -> int:
    """Restart a capped ('done') timer: reset fire_count to 0 so it runs its
    full cap again, re-activate it, and rebind the clock. The `r` key uses this
    when the selected timer has reached its fire cap."""
    cur = conn.execute(
        "UPDATE timers SET active=1, fire_count=0, last_fired_at=?, bound_at=? "
        "WHERE id=?", (_now(now), _now(now), timer_id))
    conn.commit()
    return cur.rowcount


def restore_session_timers(conn, iterm_session_id,
                           now: Optional[float] = None) -> int:
    cur = conn.execute(
        "UPDATE timers SET active=1, last_fired_at=?, bound_at=? "
        "WHERE iterm_session_id=?",
        (_now(now), _now(now), iterm_session_id))
    conn.commit()
    return cur.rowcount


def deactivate_all_timers(conn) -> None:
    conn.execute("UPDATE timers SET active=0")
    conn.commit()


def restore_all_present_timers(conn, present_sids,
                               now: Optional[float] = None) -> None:
    for sid in present_sids:
        restore_session_timers(conn, sid, now=now)


# --- clean helpers ---------------------------------------------------------------

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


def delete_tasks_for_owner(conn, owner: str) -> int:
    """Delete every task owned by `owner` (any state). Used by wipe to remove a
    dead session's work outright, vs reset_owner_tasks which only resets."""
    cur = conn.execute("DELETE FROM tasks WHERE owner=?", (owner,))
    conn.commit()
    return cur.rowcount


def delete_tasks_by_ids(conn, ids) -> int:
    """Delete the given task ids (used by wipe so the delete matches the plan
    exactly). Empty ids -> no-op."""
    ids = list(ids)
    if not ids:
        return 0
    q = ",".join("?" for _ in ids)
    cur = conn.execute(f"DELETE FROM tasks WHERE id IN ({q})", ids)
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
    # Threads too, or a wipe leaves discussions whose participants no longer
    # exist: the watcher would keep evaluating them every tick and could still
    # ping the operator about a project that was deliberately erased.
    conn.execute("DELETE FROM threads WHERE project=?", (project,))
    conn.commit()
    return (nt, ns, nm)


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


def delete_undelivered_to(conn, name: str) -> int:
    cur = conn.execute(
        "DELETE FROM messages WHERE to_name=? AND delivered_at IS NULL",
        (name,))
    conn.commit()
    return cur.rowcount


def prune_threads(conn, older_than_days: float, now=None) -> int:
    """Drop CLOSED discussions past the retention window.

    Open threads are never pruned however old, for the same reason queued
    messages are not: they are live state, and deleting a conversation still in
    flight would strand its participants. A closed one is history, and its
    transcript ages out with prune_messages anyway - leaving the thread row
    behind would mean a discussion whose posts are gone."""
    cutoff = _now(now) - older_than_days * 86400
    cur = conn.execute(
        "DELETE FROM threads WHERE state != 'open' AND closed_at > 0 "
        "AND closed_at < ?", (cutoff,))
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


# --- prs -----------------------------------------------------------------------

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
    duplicates what needs attention into a strip above the list instead.

    `since` windows OUT stale settled history the same way prune_prs ages it
    out, but never an open PR: the PR most likely to need a human is the one
    nobody has touched in a week, so it must not silently vanish from the
    listing just because it sat quiet past the retention window."""
    q = "SELECT * FROM prs WHERE 1=1"
    p: list = []
    if project:
        q += " AND project = ?"
        p.append(project)
    if owner:
        q += " AND owner = ?"
        p.append(owner)
    if since is not None:
        q += " AND (updated_at >= ? OR state NOT IN ('merged','closed'))"
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
