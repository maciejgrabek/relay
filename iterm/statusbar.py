"""Relay iTerm2 status-bar component - the per-tab arm badge.

The LIVE half (registering the component, the render/click RPCs) lives in the
watcher, which owns the iTerm2 connection and the real per-session arm state.
This module holds only the pure label composition, so it is unit-testable with
no iTerm2 dependency (like gates.py / swarm.py).

Design (validated by spikes/statusbar_spike.py):
  - iTerm2's status-bar RPC returns PLAIN text - no dynamic text color - so the
    per-mode color comes from a colored EMOJI circle (its color is intrinsic).
  - A click is a physical human action, so it is an un-spoofable channel to
    arm/disarm (a Claude session cannot click a status bar).
  - Relay never acts on its OWN panel tab, so that tab shows a neutral badge and
    its click does nothing.
"""
from __future__ import annotations

import json
import os
import time

# Colored circle per arm mode. The emoji carry their own color regardless of
# the status bar's text-color setting - green/amber/red by mode.
MODE_CIRCLE = {
    "off":    "⚪",       # white circle
    "safe":   "\U0001f7e2",   # green circle
    "wild":   "\U0001f7e1",   # yellow circle
    "insane": "\U0001f534",   # red circle
    "shadow": "\U0001f535",   # blue circle - observing, not acting
}

_ROLE_SHORT = {"coordinator": "coord", "worker": "work"}

# What each arm mode PRINTS on the badge. The "off" mode renders as "manual",
# NOT "off": relay is running, this tab just isn't armed. The word "off" is
# reserved for OFFLINE_LABEL (relay not running) so the two can never be
# confused - a live-but-unarmed tab reading "RELAY:off" looked identical to
# relay being down. "manual" matches the TUI's MANUAL arm label.
MODE_TEXT = {"off": "manual", "safe": "safe", "wild": "wild",
             "insane": "insane", "shadow": "shadow"}


def label(mode, *, own_panel=False, name=None, role=None) -> str:
    """The status-bar string for one tab.

    own_panel  -> this is relay's own panel tab; show a neutral, non-actionable
                  badge (relay never controls itself).
    name/role  -> set when the tab is a registered swarm session; appended as
                  "name (role)" so the bar doubles as a swarm identity badge.
    """
    if own_panel:
        return "⬛ RELAY: panel"       # black square - inert, this is relay
    circle = MODE_CIRCLE.get(mode, MODE_CIRCLE["off"])
    text = f"{circle} RELAY:{MODE_TEXT.get(mode, mode)}"
    if name:
        r = _ROLE_SHORT.get(role, role) if role else None
        text += f" · {name}" + (f" ({r})" if r else "")
    return text


# --- published state: relay writes, the AutoLaunch provider reads ------------
#
# iTerm2 keeps a configured status-bar component in the profile even when the
# script providing it is gone, and renders a missing provider as an ERROR. So
# the provider must outlive relay: an AutoLaunch script serves the badge
# always, reading the state relay publishes each tick. Stale or missing state
# means relay is off - the badge says so instead of erroring.

STATE_STALE_S = 5.0                     # > watcher tick (2s), < human patience
# Black circle + the word "off" = relay is NOT running. This is the ONLY badge
# that says "off"; a live-but-unarmed tab says "manual" (see MODE_TEXT), so the
# two states are never confused.
OFFLINE_LABEL = "⚫ RELAY off"


def state_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_STATUSBAR_STATE", "~/.relay/statusbar.json"))


def clicks_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_STATUSBAR_CLICKS",
                       "~/.relay/statusbar-clicks.jsonl"))


def write_state(labels: dict, now=None, path=None) -> None:
    """Atomically publish {session_id: label}. tmp + os.replace so the
    provider never reads a torn file."""
    p = path or state_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": time.time() if now is None else now,
                   "labels": labels}, f)
    os.replace(tmp, p)


def clear_state(path=None) -> None:
    """Best-effort removal on relay quit, so the badge flips to OFFLINE_LABEL
    immediately instead of waiting out the staleness window."""
    try:
        os.remove(path or state_path())
    except OSError:
        pass


def state_fresh(now=None, path=None) -> bool:
    """True while relay is live (published within STATE_STALE_S). Never
    raises."""
    try:
        with open(path or state_path()) as f:
            ts = float(json.load(f).get("ts", 0))
    except Exception:
        return False
    t = time.time() if now is None else now
    return (t - ts) <= STATE_STALE_S


def read_state_label(session_id: str, now=None, path=None) -> str:
    """The provider's badge text for one tab: the published label while relay
    is live, label('off') for a live-but-unknown tab, OFFLINE_LABEL when relay
    is off (missing/stale/garbled state). Never raises."""
    try:
        with open(path or state_path()) as f:
            d = json.load(f)
        ts = float(d.get("ts", 0))
        t = time.time() if now is None else now
        if (t - ts) > STATE_STALE_S:
            return OFFLINE_LABEL
        return d.get("labels", {}).get(session_id) or label("off")
    except Exception:
        return OFFLINE_LABEL


# --- provider heartbeat ------------------------------------------------------
#
# The AutoLaunch symlink existing does NOT mean the provider script is
# running (install.sh links it, but iTerm2 must still start it). The provider
# touches this file from its render callback; relay registers its own
# in-process component unless the heartbeat is FRESH - so a linked-but-not-
# started provider never leaves the badge slot erroring.

PROVIDER_ALIVE_MAX_AGE_S = 15.0


def provider_alive_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_STATUSBAR_ALIVE",
                       "~/.relay/statusbar-provider.alive"))


def touch_provider_alive(path=None) -> None:
    """Heartbeat write (the provider calls this, throttled). Never raises."""
    p = path or provider_alive_path()
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def provider_alive(now=None, path=None,
                   max_age=PROVIDER_ALIVE_MAX_AGE_S) -> bool:
    """True when the AutoLaunch provider heartbeat is fresh. Never raises."""
    try:
        ts = os.path.getmtime(path or provider_alive_path())
    except OSError:
        return False
    t = time.time() if now is None else now
    return (t - ts) <= max_age


# --- provider installed: the STABLE ownership signal -------------------------
#
# Exactly ONE thing may register the "com.relay.arm" RPC - iTerm2 rejects a
# second registration with DUPLICATE_SERVER_ORIGINATED_RPC, which freezes the
# badge. relay decides whether to render the badge itself by whether the
# AutoLaunch provider is INSTALLED (its symlink exists), NOT by the heartbeat.
# The heartbeat lags a just-launched-but-not-yet-rendered provider, so keying
# on it made relay double-register and freeze; the symlink is a stable
# filesystem fact with no such race. Installed -> the provider owns the badge,
# relay never registers. Not installed -> relay is the sole owner, safe to
# render in-process.

def autolaunch_link_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_STATUSBAR_AUTOLAUNCH",
                       "~/Library/Application Support/iTerm2/Scripts/"
                       "AutoLaunch/relay_statusbar.py"))


def provider_installed(path=None) -> bool:
    """True when the AutoLaunch provider symlink/file is present. Uses lexists
    so a symlink counts even if its target is momentarily missing. Never
    raises."""
    try:
        return os.path.lexists(path or autolaunch_link_path())
    except OSError:
        return False


def provider_script_path(path=None) -> str:
    """The real provider script to launch when starting it ourselves. Resolves
    the AutoLaunch symlink to its target (so we run the linked copy relay update
    keeps current), falling back to statusbar_autolaunch.py next to this module
    when nothing is installed. Never raises."""
    link = path or autolaunch_link_path()
    try:
        if os.path.lexists(link):
            return os.path.realpath(link)
    except OSError:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "statusbar_autolaunch.py")


def plan_provider_start(installed: bool, alive: bool, has_cookie: bool) -> str:
    """Pure verdict for the auto-start path (statusbar_ensure): what to do about
    the AutoLaunch provider given the observable facts.

      'absent'    - not installed; relay renders the badge in-process, nothing
                    to start.
      'alive'     - a provider is already running; leave it be (starting a
                    second would DUPLICATE-register and freeze the badge).
      'no-cookie' - it should start but iTerm2 handed out no cookie (iTerm2 not
                    running / scripting refused); can't launch.
      'start'     - installed, dead, cookie in hand: launch it now.
    """
    if not installed:
        return "absent"
    if alive:
        return "alive"
    if not has_cookie:
        return "no-cookie"
    return "start"


# --- click queue: the AutoLaunch provider writes, relay consumes -------------

def append_click(session_id: str, now=None, path=None) -> None:
    """Queue one badge click for the running relay to apply (with its usual
    guards). One JSON line per click."""
    p = path or clicks_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"ts": time.time() if now is None else now,
                            "session_id": session_id}) + "\n")


def consume_clicks(now=None, path=None, max_age=STATE_STALE_S) -> list:
    """Read and clear queued clicks, oldest first, dropping stale or garbled
    lines. The file is renamed away before reading, so each click is applied
    at most once even if relay crashes mid-consume. Never raises."""
    p = path or clicks_path()
    if not os.path.exists(p):
        return []
    work = p + ".consuming"
    try:
        os.replace(p, work)
    except OSError:
        return []
    out = []
    t = time.time() if now is None else now
    try:
        with open(work) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    sid = d.get("session_id")
                    if sid and (t - float(d.get("ts", 0))) <= max_age:
                        out.append(sid)
                except Exception:
                    continue
    except OSError:
        pass
    try:
        os.remove(work)
    except OSError:
        pass
    return out
