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
MESSAGE_KINDS = ("info", "done", "blocked", "escalation", "wake")

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
  kind TEXT NOT NULL DEFAULT 'info'
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
  updated_at REAL NOT NULL
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
  created_at REAL NOT NULL DEFAULT 0
);
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
    conn.executescript(_SCHEMA)
    # Schema versioning: 0 = fresh (CREATEs above built the current schema),
    # otherwise migrate step by step. v2 added sessions.arm_request, v3 added
    # sessions.mode (persisted arm level, so a relay restart doesn't disarm a
    # live swarm), v4 added sessions.workdir/spawn_prompt/closed_at (restore
    # context for a dead session, and whether it's closed), v5 added
    # messages.kind and sessions.worktree_repo.
    _migrate(conn)
    return conn


_CURRENT_VERSION = 6
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
            except sqlite3.OperationalError:
                pass  # column already present (interrupted earlier migration)
        v += 1
        conn.execute(f"PRAGMA user_version = {v}")


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
                  kind: str = "info") -> int:
    cur = conn.execute(
        """INSERT INTO messages(project, from_name, to_name, body, created_at,
                                kind)
           VALUES(?,?,?,?,?,?)""",
        (project, from_name, to_name, body, _now(now), kind or "info"))
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
              active=1, max_fires=10, now: Optional[float] = None) -> int:
    cur = conn.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, enabled, active, last_fired_at, bound_at, max_fires, "
        "fire_count, created_at) VALUES(?,?,?,?,?,1,?,?,?,?,0,?)",
        (iterm_session_id, label, int(interval_min), payload, mode,
         int(active), _now(now), _now(now), int(max_fires), _now(now)))
    conn.commit()
    return cur.lastrowid


def list_timers(conn, iterm_session_id) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM timers WHERE iterm_session_id=? ORDER BY id",
        (iterm_session_id,)).fetchall()


def all_timers(conn) -> List[sqlite3.Row]:
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


def mark_timer_fired(conn, timer_id, now: Optional[float] = None) -> None:
    """Record a REAL fire: advance the clock AND consume one of the fire cap.
    The 'fire now' key and the dry-run path set last_fired_at via update_timer
    instead, so they do not consume the cap."""
    conn.execute(
        "UPDATE timers SET last_fired_at=?, fire_count=fire_count+1 WHERE id=?",
        (_now(now), timer_id))
    conn.commit()


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
    conn.commit()
    return (nt, ns, nm)


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
