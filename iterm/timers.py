"""Session timers - pure scheduling logic (no iterm2/sqlite imports).

A timer fires a payload string into a session every `interval_min` minutes. This
module decides WHEN (due), WHETHER (firable, given pause/arm/ready), and when a
binding is too old to trust (needs_reconfirm). The DB stores timers; the watcher
does the sending. Pure, so it is unit-testable standalone like gates.py.

A "timer" is any mapping with keys enabled/active/interval_min/mode/
last_fired_at/bound_at (sqlite3.Row and plain dict both work via t["key"]).
"""
from __future__ import annotations

INTERVAL_MIN = 1
INTERVAL_MAX = 90
MODES = ("idle", "now")
_DAY = 86400.0


def clamp_interval(n) -> int:
    """Coerce to an int minute count within [1, 90]. Junk -> 1."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return INTERVAL_MIN
    return max(INTERVAL_MIN, min(INTERVAL_MAX, v))


def sanitize_payload(s) -> str:
    """Single-line payload: any newline becomes a space, ends trimmed - so a
    payload can never carry an embedded Enter that submits early."""
    if not s:
        return ""
    return " ".join(str(s).split("\n")).replace("\r", " ").strip()


def is_due(timer, now) -> bool:
    if not (timer["enabled"] and timer["active"]):
        return False
    return (timer["last_fired_at"] or 0) + timer["interval_min"] * 60 <= now


def due_timers(timers, now) -> list:
    return [t for t in timers if is_due(t, now)]


def firable(timer, *, ready, paused, armed, require_armed) -> bool:
    """Fire gate for an already-due timer. Pause freezes all; require_armed
    blocks unarmed sessions; idle mode waits for a ready prompt, now does not."""
    if paused:
        return False
    if require_armed and not armed:
        return False
    if timer["mode"] == "idle" and not ready:
        return False
    return True


def next_due_in(timer, now) -> float:
    return (timer["last_fired_at"] or 0) + timer["interval_min"] * 60 - now


def needs_reconfirm(timer, now, reconfirm_days) -> bool:
    """True when the binding is older than the re-confirm window - a stale
    session_id (recycled UUID) guard. Disabled when reconfirm_days <= 0."""
    if not reconfirm_days or reconfirm_days <= 0:
        return False
    return now - (timer["bound_at"] or 0) > reconfirm_days * _DAY
