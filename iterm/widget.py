"""Relay desktop widget - published state (relay writes, the widget reads).

The floating mascot window is a separate process, so relay hands it state
through a file rather than an API: `write_state` publishes atomically each
render tick, the widget polls at ~1s, and anything older than STALE_S means
relay is not running. That is the same publish/poll contract `statusbar.py`
uses for the iTerm2 arm badge, for the same reason - no daemon, no socket, no
handshake, and a consumer that degrades to "off" on its own.

Pure by design (no iterm2 / textual / sqlite imports), so it is unit-testable
standalone like gates.py and timers.py. It renders nothing and decides nothing
about the mascot: app.py hands it the state, colour and art the banner already
computed, so the widget cannot disagree with the panel.

See docs/specs/2026-07-28-desktop-widget-design.md.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

# Longer than the TUI's 0.5s render tick and the watcher's 2s poll, shorter than
# human patience. Matches statusbar.STATE_STALE_S deliberately: two consumers
# disagreeing about what "relay is off" means would be a bug waiting to happen.
STALE_S = 5.0

# app._speech_bubble renders the text row as "<attach> <text> |". The widget's
# compact mode has no creature to read the sentence out of, so it is published
# as its own field rather than left buried in the art.
_SPEECH_RE = re.compile(r"◃\s*(.*?)\s*│")
# Textual markup would render as literal "[bold]" inside the widget's <pre>.
# The mascot frames are built bracket-free, so this is belt-and-braces.
_MARKUP_RE = re.compile(r"\[/?[^\]]*\]")


def state_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_WIDGET_STATE", "~/.relay/widget.json"))


def speech(art) -> str:
    """The sentence inside the mascot's speech bubble, or '' if there is none."""
    for line in art or ():
        m = _SPEECH_RE.search(line)
        if m:
            return m.group(1)
    return ""


def payload(state: str, color: str, art: List[str], *, armed: int,
            awaiting: int, working: bool, paused: bool, band: str,
            sessions: int, panel_sid: Optional[str] = None,
            attention_sid: Optional[str] = None,
            now: Optional[float] = None) -> dict:
    """Build what the widget reads.

    Both halves cross the boundary and do different jobs: `art` is the mascot
    block already rendered by app.mascot_face_big (so every skin and state works
    with no duplication on the widget side), while `state`/`color`/the counts
    drive the window's frame, tint and alarm pulse.

    `panel_sid` is relay's own iTerm2 session id, which the widget's wordmark
    uses to bring you back to the panel. `attention_sid` is the session that
    needs a human right now, if any - so clicking the creature while it is
    alarmed takes you to the thing it is alarmed about, rather than making you
    go to the panel and hunt for it.
    """
    clean = [_MARKUP_RE.sub("", l).rstrip() for l in (art or ())]
    return {
        "ts": time.time() if now is None else now,
        "state": state,
        "color": color,
        "art": clean,
        "phrase": speech(clean),
        "armed": int(armed),
        "awaiting": int(awaiting),
        "working": bool(working),
        "paused": bool(paused),
        "band": band,
        "sessions": int(sessions),
        "panel_sid": panel_sid,
        "attention_sid": attention_sid,
    }


def write_state(data: dict, path: Optional[str] = None) -> None:
    """Publish atomically - tmp + os.replace, so a 1s poll can never catch a
    half-written file."""
    p = path or state_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def clear_state(path: Optional[str] = None) -> None:
    """Best-effort removal on quit, so the creature greys out immediately
    instead of waiting out the staleness window."""
    try:
        os.remove(path or state_path())
    except OSError:
        pass


def state_fresh(now: Optional[float] = None, path: Optional[str] = None) -> bool:
    """True while relay is live. Missing, stale or garbled all read as off -
    never raises, because the caller is a render loop."""
    try:
        with open(path or state_path()) as f:
            ts = float(json.load(f).get("ts", 0))
    except Exception:
        return False
    return ((time.time() if now is None else now) - ts) <= STALE_S
