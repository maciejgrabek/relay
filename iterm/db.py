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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  name TEXT PRIMARY KEY,
  iterm_session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  status_text TEXT NOT NULL DEFAULT '',
  registered_at REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  from_name TEXT NOT NULL,
  to_name TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at REAL NOT NULL,
  delivered_at REAL
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
    # Stamp the schema version on a fresh DB (0 = never stamped) so future
    # migrations have a baseline to branch on.
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute("PRAGMA user_version = 1")
    return conn


def _now(now: Optional[float]) -> float:
    return now if now is not None else time.time()


# --- sessions ----------------------------------------------------------------

def register(conn, name: str, iterm_session_id: str, role: str,
             project: str = "", now: Optional[float] = None) -> None:
    """Insert or rebind a named session. Re-registering an existing name
    updates the binding (a respawned worker reclaims its identity) but keeps
    the original registered_at."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    t = _now(now)
    conn.execute(
        """INSERT INTO sessions(name, iterm_session_id, role, project,
                                status_text, registered_at, last_seen)
           VALUES(?,?,?,?,'',?,?)
           ON CONFLICT(name) DO UPDATE SET
             iterm_session_id=excluded.iterm_session_id,
             role=excluded.role, project=excluded.project,
             last_seen=excluded.last_seen""",
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


def list_sessions(conn, project: Optional[str] = None) -> List[sqlite3.Row]:
    if project is None:
        return conn.execute(
            "SELECT * FROM sessions ORDER BY registered_at").fetchall()
    return conn.execute(
        "SELECT * FROM sessions WHERE project=? ORDER BY registered_at",
        (project,)).fetchall()


# --- messages ------------------------------------------------------------------

def queue_message(conn, from_name: str, to_name: str, body: str,
                  project: str = "", now: Optional[float] = None) -> int:
    cur = conn.execute(
        """INSERT INTO messages(project, from_name, to_name, body, created_at)
           VALUES(?,?,?,?,?)""",
        (project, from_name, to_name, body, _now(now)))
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
