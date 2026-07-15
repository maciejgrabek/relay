"""Relay swarm - pure decision logic (no iTerm2, no sqlite imports).

Like gates.py, this is the load-bearing logic kept pure so it can be
unit-tested: which tasks a completion unblocks, what wake-up messages say,
whether a screen tail is Claude's idle input box (safe to inject into), and
when a session counts as stale. Rows come in as dicts/sqlite Rows; both
support [] access.
"""
from __future__ import annotations

import re
from typing import List, Optional


def parse_blockers(s: Optional[str]) -> List[int]:
    if not s:
        return []
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def unblocked_by_completion(tasks, done_id: int) -> list:
    """Tasks that list done_id as a blocker, are not done themselves, and
    whose blockers are now ALL done. Call AFTER the done task's row was
    updated, passing the full (project-wide or global) task list."""
    state = {t["id"]: t["state"] for t in tasks}
    out = []
    for t in tasks:
        if t["state"] == "done":
            continue
        blockers = parse_blockers(t["blocked_by"])
        if done_id in blockers and all(state.get(b) == "done" for b in blockers):
            out.append(t)
    return out


# --- wake-up message bodies (queued as from_name='relay') ---------------------

def wakeup_assignment_body(task) -> str:
    b = f"task #{task['id']} assigned to you: {task['title']}"
    if task["spec_path"]:
        b += (f". Spec: {task['spec_path']} - read it, split it into subtasks "
              f"(relay task add --parent {task['id']} \"...\"), then execute "
              f"them and keep states updated")
    return b


def wakeup_unblocked_body(task) -> str:
    return (f"task #{task['id']} '{task['title']}' is unblocked - all its "
            f"blockers are done. Set it to doing and start")


def delivery_text(from_name: str, body: str) -> str:
    """The literal text typed into the target session. Newlines flattened so
    the injected turn is one paste + one Enter (bracketed-paste lesson)."""
    flat = " ".join(str(body).splitlines())
    return f"[relay msg from {from_name}] {flat}"


# --- injection safety: is this Claude's idle input box? -----------------------

# Claude Code idle screens end with a bordered input box ("│ > ") and/or the
# shortcuts footer. A bare shell prompt has neither - and injecting a message
# into a SHELL would execute it as a command, so default to NOT ready.
_INPUT_BOX_RE = re.compile(r"^\s*│\s*>")
_READY_MARKERS = ("? for shortcuts", "⏵⏵")


def claude_prompt_ready(lines: List[str]) -> bool:
    tail = [l for l in lines[-15:] if l.strip()]
    for l in tail:
        if _INPUT_BOX_RE.match(l):
            return True
        if any(m in l for m in _READY_MARKERS):
            return True
    return False


# --- staleness ---------------------------------------------------------------

def stale_reason(now: float, threshold_s: float,
                 oldest_undelivered_ts: Optional[float] = None,
                 doing_since: Optional[float] = None,
                 screen_changed_ts: Optional[float] = None) -> Optional[str]:
    """Why a session counts as stale, or None. Two triggers (spec section 6):
    a queued message nobody could deliver for threshold_s, or an owned 'doing'
    task with no screen activity for threshold_s."""
    if oldest_undelivered_ts is not None:
        waited = now - oldest_undelivered_ts
        if waited > threshold_s:
            return f"queued message undelivered for {int(waited / 60)}m"
    if doing_since is not None:
        quiet_since = screen_changed_ts if screen_changed_ts is not None else doing_since
        quiet = now - quiet_since
        if quiet > threshold_s:
            return f"no activity for {int(quiet / 60)}m while a task is 'doing'"
    return None


# --- swarm view rendering (plain text; the TUI Static uses markup=False) ------

_STATE_COLS = ("todo", "doing", "blocked", "done")


def _clip(s: str, w: int) -> str:
    s = str(s)
    return s if len(s) <= w else s[: max(0, w - 1)] + "…"


def render_swarm(sessions, tasks, messages, now: float, width: int = 100) -> str:
    """One plain-text screen: roster, kanban board, epic progress, message
    feed. Grouped by project when more than one is present."""
    out: List[str] = []
    projects = sorted({s["project"] for s in sessions}
                      | {t["project"] for t in tasks}) or [""]
    for proj in projects:
        p_sessions = [s for s in sessions if s["project"] == proj]
        p_tasks = [t for t in tasks if t["project"] == proj]
        coord = next((s["name"] for s in p_sessions
                      if s["role"] == "coordinator"), "-")
        workers = sum(1 for s in p_sessions if s["role"] == "worker")
        out.append(f"PROJECT {proj or '(none)'} · coordinator: {coord} · "
                   f"{workers} workers")
        for s in p_sessions:
            out.append(f"  {s['name']:<16} {s['role']:<12} "
                       f"{_clip(s['status_text'] or '-', width - 32)}")
        out.append("")

        # kanban: 4 columns of "#id title"
        colw = max(12, (width - 3 * 3) // 4)
        cols = {st: [f"#{t['id']} {_clip(t['title'], colw - len(str(t['id'])) - 2)}"
                     for t in p_tasks if t["state"] == st]
                for st in _STATE_COLS}
        height = max([len(v) for v in cols.values()] + [1])
        out.append("   ".join(h.upper().ljust(colw)
                              for h in _STATE_COLS))
        out.append("   ".join("─" * colw for _ in _STATE_COLS))
        for i in range(height):
            out.append("   ".join(
                (cols[st][i] if i < len(cols[st]) else "").ljust(colw)
                for st in _STATE_COLS))
        out.append("")

        # epic progress: children done/total
        epics = [t for t in p_tasks if t["parent_id"] is None]
        for e in epics:
            kids = [t for t in p_tasks if t["parent_id"] == e["id"]]
            if kids:
                done = sum(1 for k in kids if k["state"] == "done")
                out.append(f"  EPIC #{e['id']} {_clip(e['title'], width - 30)}"
                           f"  {done}/{len(kids)}")
        out.append("")

    out.append("MESSAGES")
    for m in messages[-8:]:
        q = "" if m["delivered_at"] else "  [queued]"
        out.append(f"  {m['from_name']} -> {m['to_name']}: "
                   f"{_clip(m['body'], width - 30)}{q}")
    if not messages:
        out.append("  (none)")
    return "\n".join(out)
