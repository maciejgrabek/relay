"""Relay-iTerm audit log - a durable record of what Relay decided unattended.

Logs the unattended decisions you'd want to review after walking away:
  - "auto-approved" : Relay sent Enter on a safe prompt in an armed session
  - "escalated"     : Relay refused to auto-act (dangerous / question / unsure)
  - "would-approve" : dry-run; what Relay WOULD have approved (no Enter sent)
  - "delivered"     : Relay typed a queued swarm message into an idle session
  - "would-deliver" : dry-run; what Relay WOULD have delivered
Manual keypresses are NOT logged - those are your deliberate actions.

One JSON object per line in ~/.relay/audit.jsonl. Durability matters here (the
one event you can't afford to lose is an unattended approval), so:
  - appends + prune take an exclusive file lock (flock) to serialize across
    concurrent Relay instances,
  - record() fsyncs and RETURNS success so the caller can react to a failed
    write rather than silently approving unlogged,
  - prune PRESERVES unparseable lines (corruption is itself evidence) instead of
    deleting them.

Retention: RETENTION_DAYS (default 7), pruned once when the TUI starts.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

try:
    import fcntl  # POSIX only; macOS/Linux have it
    _HAVE_FCNTL = True
except Exception:  # pragma: no cover
    _HAVE_FCNTL = False

AUDIT_PATH = os.path.expanduser(
    os.environ.get("RELAY_AUDIT_LOG", "~/.relay/audit.jsonl"))
RETENTION_DAYS = float(os.environ.get("RELAY_AUDIT_RETENTION_DAYS", "7"))

VALID_VERDICTS = ("auto-approved", "escalated", "would-approve",
                  "delivered", "would-deliver")


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)


class _Lock:
    """Exclusive advisory lock on a sidecar file, held for the with-block.
    No-op if fcntl is unavailable. Best-effort - never raises."""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        if not _HAVE_FCNTL:
            return self
        try:
            _ensure_dir()
            self._fh = open(AUDIT_PATH + ".lock", "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
        return False


def record(verdict: str, session: str, command: str, reason: str,
           now: Optional[float] = None) -> bool:
    """Append one audit entry. Returns True on a durable write, False on failure
    (so the caller can warn rather than silently proceed). Never raises."""
    try:
        entry = {
            "ts": now if now is not None else time.time(),
            "verdict": verdict,
            "session": (session or "")[:200],
            "command": (command or "")[:500],
            "reason": (reason or "")[:300],
        }
        line = json.dumps(entry) + "\n"
        with _Lock():
            with open(AUDIT_PATH, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return True
    except Exception:
        return False


def prune_old(now: Optional[float] = None) -> int:
    """Drop entries older than RETENTION_DAYS. Returns how many were removed.
    Unparseable / non-JSON lines are KEPT (corruption is evidence). Entries
    missing a numeric ts are kept (we can't prove they're old). Atomic replace
    under the lock; never raises."""
    if not os.path.exists(AUDIT_PATH):
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_DAYS * 86400
    try:
        with _Lock():
            kept, dropped = [], 0
            with open(AUDIT_PATH) as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        ts = json.loads(line).get("ts")
                    except Exception:
                        kept.append(line)      # keep corruption, don't erase it
                        continue
                    if not isinstance(ts, (int, float)):
                        kept.append(line)      # no usable ts -> can't prove old
                    elif ts >= cutoff:
                        kept.append(line)
                    else:
                        dropped += 1
            if dropped:
                tmp = AUDIT_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(kept) + ("\n" if kept else ""))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, AUDIT_PATH)
            return dropped
    except Exception:
        return 0
