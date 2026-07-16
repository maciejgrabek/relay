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

# Colored circle per arm mode. The emoji carry their own color regardless of
# the status bar's text-color setting - green/amber/red by mode.
MODE_CIRCLE = {
    "off":    "⚪",       # white circle
    "safe":   "\U0001f7e2",   # green circle
    "wild":   "\U0001f7e1",   # yellow circle
    "insane": "\U0001f534",   # red circle
}

_ROLE_SHORT = {"coordinator": "coord", "worker": "work"}


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
    text = f"{circle} RELAY:{mode}"
    if name:
        r = _ROLE_SHORT.get(role, role) if role else None
        text += f" · {name}" + (f" ({r})" if r else "")
    return text
