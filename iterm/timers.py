"""Session timers - pure scheduling logic (no iterm2/sqlite imports).

A timer fires a payload string into a session every `interval_min` minutes. This
module decides WHEN (due), WHETHER (firable, given pause/arm/ready), and when a
binding is too old to trust (needs_reconfirm). The DB stores timers; the watcher
does the sending. Pure, so it is unit-testable standalone like gates.py.

A "timer" is any mapping with keys enabled/active/interval_min/mode/
last_fired_at/bound_at (sqlite3.Row and plain dict both work via t["key"]).
The fire-cap fields max_fires/fire_count are read defensively (via _field) so a
row or fixture that predates them still works (missing -> 0 -> unlimited).
"""
from __future__ import annotations

INTERVAL_MIN = 1
INTERVAL_MAX = 90
MODES = ("idle", "now")
DEFAULT_MAX_FIRES = 10
_DAY = 86400.0


def _field(timer, key, default):
    """Read an optional timer field, defaulting when the key is absent (old row
    /fixture) or NULL. Works for both sqlite3.Row and plain dict."""
    try:
        v = timer[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


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


def capped(timer) -> bool:
    """True when a fire-limited timer has reached its cap: max_fires > 0 and
    fire_count >= max_fires. max_fires == 0 means unlimited (never capped)."""
    mf = _field(timer, "max_fires", 0)
    return mf > 0 and _field(timer, "fire_count", 0) >= mf


def fires_left(timer):
    """Remaining fires before the cap, or None for unlimited (max_fires == 0)."""
    mf = _field(timer, "max_fires", 0)
    if mf <= 0:
        return None
    return max(0, mf - _field(timer, "fire_count", 0))


def is_due(timer, now) -> bool:
    if not (timer["enabled"] and timer["active"]) or capped(timer):
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
