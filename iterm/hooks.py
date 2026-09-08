"""Relay's entries in Claude Code's user settings, and how they are merged.

Claude Code runs hooks from ~/.claude/settings.json (user scope: every
session on the machine, present and future - there is no per-session
registration). Relay needs two: the sender's PostToolUse on SendMessage and
the recipient's UserPromptSubmit, both async so they never cost a session a
millisecond, both running `relay hook <event>` which is silent and exit-0.

Everything here is pure dict work so test_hooks.py runs standalone; cli.py
does the file I/O, the diff and the asking.
"""
from __future__ import annotations

import copy
import difflib
import json
from typing import Dict, List

_PREFIX = "relay hook "

ENTRIES: Dict[str, List[dict]] = {
    "PostToolUse": [{"matcher": "SendMessage",
                     "hooks": [{"type": "command",
                                "command": "relay hook post-tool",
                                "async": True}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
                                     "command": "relay hook prompt",
                                     "async": True}]}],
}


def is_ours(hook: dict) -> bool:
    return (isinstance(hook, dict)
            and str(hook.get("command", "")).startswith(_PREFIX))


def _groups(settings: dict, event: str) -> List[dict]:
    g = (settings.get("hooks") or {}).get(event) or []
    return [x for x in g if isinstance(x, dict)]


def status(settings: dict) -> Dict[str, str]:
    """Per event: ok (exactly our current group present), stale (a relay
    hook is there but not our current one), missing."""
    out = {}
    for event, ours in ENTRIES.items():
        groups = _groups(settings, event)
        if any(grp == ours[0] for grp in groups):
            out[event] = "ok"
        elif any(is_ours(h) for grp in groups for h in grp.get("hooks", [])):
            out[event] = "stale"
        else:
            out[event] = "missing"
    return out


def strip(settings: dict) -> dict:
    """The settings without any relay hook. A group that also holds a
    foreign hook keeps that hook; an event left with no groups is dropped;
    an empty hooks dict is dropped. Never mutates the input."""
    s = copy.deepcopy(settings)
    hooks = s.get("hooks")
    if not isinstance(hooks, dict):
        return s
    for event in list(hooks):
        kept = []
        for grp in hooks[event] or []:
            if not isinstance(grp, dict):
                kept.append(grp)
                continue
            inner = [h for h in grp.get("hooks", []) if not is_ours(h)]
            if inner:
                grp = dict(grp, hooks=inner)
                kept.append(grp)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del s["hooks"]
    return s


def merge(settings: dict) -> dict:
    """The settings with relay's current entries: any older relay hook is
    removed first, then ours are appended after whatever foreign groups the
    event already has. Idempotent."""
    s = strip(settings)
    hooks = s.setdefault("hooks", {})
    for event, ours in ENTRIES.items():
        hooks[event] = list(hooks.get(event) or []) + copy.deepcopy(ours)
    return s


def diff_text(before: dict, after: dict, path: str) -> str:
    a = json.dumps(before, indent=2, sort_keys=True).splitlines(keepends=True)
    b = json.dumps(after, indent=2, sort_keys=True).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=path, tofile=path))
