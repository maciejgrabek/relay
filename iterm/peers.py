"""The native Claude Code peer registry and message envelope, read-only.

Every running Claude Code session writes ~/.claude/sessions/<pid>.json with
its derived name, working directory, session id and the socket (a named pipe
on Windows) other sessions message it through. Relay reads that file to put
names on hook payloads; it never writes there and never opens the socket.

A received peer message reaches the recipient's UserPromptSubmit hook as:

    <cross-session-message from="uds:/tmp/cc-socks/38047.sock"
                           from-name="peera-d0" from-mode="prompting">
    body
    </cross-session-message>

Pure stdlib, no sqlite, so test_peers.py runs standalone.
"""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

import usage

_ENVELOPE = re.compile(
    r'^\s*<cross-session-message((?:\s+[a-z-]+="[^"]*")*)\s*>\n'
    r'([\s\S]*)\n</cross-session-message>\s*$')
_ATTR = re.compile(r'([a-z-]+)="([^"]*)"')


def read_registry(root: Optional[str] = None) -> List[dict]:
    """Every session Claude Code has registered: pid, session_id, name, cwd,
    socket, status. A file that is not JSON, or lacks a name or session id,
    is skipped - the registry also holds key files and half-written rows."""
    root = root or usage.sessions_root()
    out: List[dict] = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, fn)) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        name, sid = str(d.get("name") or ""), str(d.get("sessionId") or "")
        if not name or not sid:
            continue
        out.append({
            "pid": int(d.get("pid") or 0),
            "session_id": sid,
            "name": name,
            "cwd": str(d.get("cwd") or ""),
            "socket": str(d.get("messagingSocketPath") or ""),
            "status": str(d.get("status") or ""),
        })
    return out


def _socket_key(path: str) -> str:
    """/private/tmp/x.sock and /tmp/x.sock are one socket on macOS; the
    registry writes one spelling and SendMessage may echo the other."""
    p = path[len("uds:"):] if path.startswith("uds:") else path
    if p.startswith("/private/"):
        p = p[len("/private"):]
    return p


def resolve_target(target: str, registry) -> Optional[dict]:
    """The registry entry a SendMessage target names: an exact session name,
    a `uds:` address, or a bare socket path. None for anything else, which
    is how a send to a subagent or teammate is told apart from a peer."""
    if not target:
        return None
    for e in registry:
        if e["name"] == target:
            return e
    key = _socket_key(target)
    if not key.startswith("/") and not key.startswith("\\\\"):
        return None
    for e in registry:
        if e["socket"] and _socket_key(e["socket"]) == key:
            return e
    return None


def by_session_id(session_id: str, registry) -> Optional[dict]:
    for e in registry:
        if e["session_id"] == session_id:
            return e
    return None


def parse_envelope(prompt) -> Optional[dict]:
    """The sender and body of a received peer message, or None when the
    prompt is anything else. The whole prompt must be the envelope: a prompt
    that merely mentions the tag is a human talking about it."""
    if not isinstance(prompt, str):
        return None
    m = _ENVELOPE.match(prompt)
    if not m:
        return None
    attrs = dict(_ATTR.findall(m.group(1)))
    return {"from": attrs.get("from", ""),
            "from_name": attrs.get("from-name", ""),
            "from_mode": attrs.get("from-mode", ""),
            "body": m.group(2)}


def peer_send_target(tool_name: str, tool_input, registry) -> Optional[dict]:
    """The peer a SendMessage call addressed, if it addressed one at all."""
    if tool_name != "SendMessage" or not isinstance(tool_input, dict):
        return None
    target = tool_input.get("to") or tool_input.get("recipient") or ""
    return resolve_target(str(target), registry)
