"""Relay's outbound event seam - a typed record of what relay decided, at a
path other processes can read.

One JSON object per line in ~/.relay/events.jsonl, plus an optional
fire-and-forget HTTP POST. Deliberately modelled on audit.py - same flock, same
prune, same never-raises posture - with ONE difference that matters:

  audit.record()  returns success, and callers CHANGE BEHAVIOUR when it fails
                  (watcher.py refuses to auto-approve rather than approve
                  unlogged). The audit log is load-bearing evidence.
  events.emit()   returns None, on purpose, so that no caller can branch on it.
                  A failed event write must change relay's behaviour in exactly
                  zero ways. Events are observational.

Preserve that asymmetry when editing either module.

Relay never EXECUTES anything from this seam. There is no hook file, because a
session relay supervises can write files, and relay must not run code that
lib/danger.sh never saw. See docs/specs/2026-08-21-event-seam-design.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

try:
    import fcntl  # POSIX only; macOS/Linux have it
    _HAVE_FCNTL = True
except Exception:  # pragma: no cover
    _HAVE_FCNTL = False

EVENTS_PATH = os.path.expanduser(
    os.environ.get("RELAY_EVENTS_LOG", "~/.relay/events.jsonl"))
# 0 (or less) = never prune - see prune_old().
RETENTION_DAYS = float(os.environ.get("RELAY_EVENTS_RETENTION_DAYS", "7"))

ENVELOPE_VERSION = 1

# Stage 1: exactly the kinds that already reach notify_mac, so the first
# version ships with provable parity to something that already works. This is
# NOT a runtime gate - an unrecognised kind is still written, because an event
# log that drops data is worse than one with an odd row. Its job is to give
# test_watcher's source-level parity test something to assert against.
VALID_KINDS = (
    "gate.escalated",       # relay refused to auto-act (dangerous/question/unsure)
    "arm.changed",          # armed at a level on a spawn request
    "arm.refused",          # refused an arm escalation outside the spawn window
    "audit.failed",         # audit write failed, so relay declined to act
    "session.stale",        # tab stale, or session gone with messages queued
    "escalation.received",  # a worker escalated to the human
    "task.done",            # swarm task(s) completed
    "extreme.exhausted",    # extreme budget spent, back to insane
)

POST_BODIES = ("minimal", "full")

# Set once by configure() at TUI start. The defaults MATCH config.py's defaults
# so an unconfigured import (a CLI path, a test) behaves like a default install.
_file_enabled = True
_post_url = ""
_post_body = "minimal"


def configure(*, file_enabled: bool = True, post_url: str = "",
              post_body: str = "minimal",
              retention_days: Optional[float] = None) -> None:
    """Apply the [events] config once, at TUI start. Not live-reloadable in v1:
    a mid-run config edit takes effect on the next relay start."""
    global _file_enabled, _post_url, _post_body, RETENTION_DAYS
    try:
        _file_enabled = bool(file_enabled)
    except Exception:
        pass
    try:
        _post_url = str(post_url or "").strip()
    except Exception:
        pass
    try:
        _post_body = post_body if post_body in POST_BODIES else "minimal"
    except Exception:
        pass
    if retention_days is not None:
        try:
            RETENTION_DAYS = float(retention_days)
        except (TypeError, ValueError):
            pass


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)


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
            self._fh = open(EVENTS_PATH + ".lock", "w")
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


def emit(kind: str, *, session: str = "", session_id: str = "",
         title: str = "", message: str = "", data: Optional[dict] = None,
         now: Optional[float] = None) -> None:
    """Record one event to every enabled channel.

    Returns None deliberately - see the module docstring. Never raises. The two
    channels are independent: a broken file must not cost you the POST."""
    try:
        entry = {
            "v": ENVELOPE_VERSION,
            "ts": now if now is not None else time.time(),
            "kind": str(kind)[:100],
            "session": (session or "")[:200],
            "session_id": (session_id or "")[:200],
            "title": (title or "")[:200],
            "message": (message or "")[:500],
            "data": data if isinstance(data, dict) else {},
        }
    except Exception:
        return
    _write_file(entry)
    _post(entry)


def _write_file(entry: dict) -> None:
    """Append one line. Swallows everything - see emit()."""
    if not _file_enabled:
        return
    try:
        line = json.dumps(entry) + "\n"
        with _Lock():
            with open(EVENTS_PATH, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass


def _post(entry: dict) -> None:
    """Fire-and-forget POST, exactly how notify_mac already launches
    terminal-notifier and afplay (watcher.py:163, watcher.py:178). Popen and
    walk away: this runs inside the watcher tick and must never block it.

    No retry, no queue - a queue is a daemon wearing a hat, and the file
    channel is the durable record. A lost POST is a lost ping, nothing more.

    The URL is a separate argv element and the argv is fixed: nothing the
    operator configures is ever interpreted as a command."""
    if not _post_url:
        return
    try:
        if _post_body == "full":
            body = entry
        else:
            # minimal: enough to know WHICH session wants you, and nothing
            # about what it wanted to run. gate.escalated messages carry real
            # command text - paths, connection strings, occasionally a secret
            # in an argument - and POSTing that publishes it to a third party.
            body = {k: entry[k] for k in ("v", "ts", "kind", "session")}
        subprocess.Popen(
            ["curl", "-fsS", "-m", "5", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(body), _post_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def prune_old(now: Optional[float] = None) -> int:
    """Drop entries older than RETENTION_DAYS. Returns how many were removed.
    Unparseable / non-JSON lines are KEPT (corruption is evidence). Entries
    missing a numeric ts are kept (we can't prove they're old). Atomic replace
    under the lock; never raises.

    RETENTION_DAYS <= 0 means NEVER PRUNE, matching every other duration in
    relay (power.release_after = 0 is "never release", burn.window = 0 is off).
    Without this guard the arithmetic reads 0 as "keep nothing": cutoff lands
    on now, every ts fails ts >= cutoff, and the whole log goes - including
    rows written seconds earlier."""
    if RETENTION_DAYS <= 0:
        return 0
    if not os.path.exists(EVENTS_PATH):
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_DAYS * 86400
    try:
        with _Lock():
            kept, dropped = [], 0
            with open(EVENTS_PATH) as f:
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
                tmp = EVENTS_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(kept) + ("\n" if kept else ""))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, EVENTS_PATH)
            return dropped
    except Exception:
        return 0
