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


def delivery_text(from_name: str, body: str, kind: str = "info") -> str:
    """The literal text typed into the target session. Newlines flattened so
    the injected turn is one paste + one Enter (bracketed-paste lesson).

    This text is sent as raw keystrokes, so ESC / C0 control bytes (e.g. an
    interrupt sequence) in an attacker-influenceable body would be interpreted
    by the terminal, not typed. Strip everything that isn't printable or a
    plain space after flattening."""
    flat = " ".join(str(body).splitlines())
    flat = "".join(c for c in flat if c.isprintable() or c == " ")
    tag = "msg" if kind in ("", "info") else kind
    return f"[relay {tag} from {from_name}] {flat}"


def kind_of(m) -> str:
    """A message row/dict's kind, defaulting 'info' for pre-v5 rows and plain
    dict fixtures (sqlite Row and dict both support .keys())."""
    try:
        k = m["kind"] if "kind" in m.keys() else ""
    except Exception:
        k = ""
    return k or "info"


def escalation_pings(msgs, already: set) -> list:
    """Queued messages that should ping the human NOW: kind 'escalation' and
    not already pinged. Delivery still waits for the target's idle prompt;
    the ping must not."""
    return [m for m in msgs
            if kind_of(m) == "escalation" and m["id"] not in already]


# --- live-scoped stakes: only count what relay can act on RIGHT NOW -----------
#
# The header "N msgs queued" and the quit-guard stakes must reflect this run's
# live sessions, not the whole DB. An abandoned project leaves undelivered
# messages and orphaned "doing" tasks behind; counting those cries wolf (the
# panel warns about work relay cannot deliver or supervise), and a panel you
# learn to ignore is worthless. Scope every count to the sessions relay is
# actually watching.

def live_names(registry, live_sids) -> set:
    """The swarm session NAMES relay is watching live now: registry rows
    (bare-sid -> session row) whose iterm session is currently present. A name
    absent here is stale - its tab is gone this run, so relay can neither
    deliver to it nor supervise it."""
    live = set(live_sids)
    return {row["name"] for sid, row in registry.items() if sid in live}


def live_queued_count(undelivered, names) -> int:
    """Undelivered messages addressed to a live session (deliverable this run).
    A message to a name relay isn't watching can't be delivered now, so it is
    not a queued stake for the operator."""
    live = set(names)
    return sum(1 for m in undelivered if m["to_name"] in live)


def live_doing_count(tasks, names) -> int:
    """'doing' tasks owned by a live session (in-flight this run). A doing task
    whose owner is gone is an orphan - surfaced separately via the restore/wipe
    path - not a live stake, so it is excluded here."""
    live = set(names)
    return sum(1 for t in tasks
               if t["state"] == "doing" and (t["owner"] or "") in live)


# --- injection safety: is this Claude's idle input box? -----------------------

# Claude Code idle screens end with a bordered input box ("│ > ") and/or the
# shortcuts footer. A bare shell prompt has neither - and injecting a message
# into a SHELL would execute it as a command, so default to NOT ready.
#
# Anchoring matters: after you quit claude, the input box / footer chrome
# lingers on screen a line or three ABOVE a live shell prompt. Scanning a
# 15-line tail would still see that chrome and wrongly report "ready", so we
# require the VERY LAST non-empty line to itself be Claude chrome. A shell
# prompt (or any other non-chrome line) at the bottom vetoes delivery.
_INPUT_BOX_RE = re.compile(r"^\s*│\s*>")
_READY_MARKERS = ("? for shortcuts", "⏵⏵")
_BOX_GLYPHS = set("─│╯╮╰╭┌┐└┘├┤┬┴┼")


def _is_marker_line(l: str) -> bool:
    """A footer marker or the input-box row - the 'ready' signal itself."""
    return bool(_INPUT_BOX_RE.match(l)) or any(m in l for m in _READY_MARKERS)


def _is_chrome_line(l: str) -> bool:
    """True when this line is unmistakably Claude UI chrome (never a shell
    prompt): the input-box row, a box border, or a footer marker line."""
    s = l.strip()
    if not s:
        return False
    if _is_marker_line(l):
        return True
    if s[0] in "╰╭":                      # box top/bottom corner
        return True
    if all(c in _BOX_GLYPHS for c in s):  # a pure border line
        return True
    return False


def claude_prompt_ready(lines: List[str]) -> bool:
    tail = [l for l in lines if l.strip()]
    if not tail:
        return False
    # (a) the ready signal must appear near the bottom, AND
    if not any(_is_marker_line(l) for l in tail[-3:]):
        return False
    # (b) the bottom line itself must be chrome - a shell prompt below the
    #     lingering box (ends with $, %, ❯, or anything non-chrome) vetoes.
    return _is_chrome_line(tail[-1])


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


# --- restore / clean planning (pure; rows in, plans out) ----------------------

def _nondone_ids(tasks, owner):
    return [t["id"] for t in tasks
            if t["owner"] == owner and t["state"] != "done"]


def restore_candidates(sessions, tasks, names=None):
    """Sessions to revive. Auto (names=None): closed sessions owning non-done
    work. Manual (names given): those named sessions owning non-done work,
    regardless of closed state. Sorted by name."""
    out = []
    for s in sorted(sessions, key=lambda r: r["name"]):
        if names is None:
            if not s["closed_at"]:
                continue
        elif s["name"] not in names:
            continue
        ids = _nondone_ids(tasks, s["name"])
        if not ids:
            continue
        out.append({"name": s["name"], "role": s["role"],
                    "project": s["project"], "workdir": s["workdir"],
                    "spawn_prompt": s["spawn_prompt"], "task_ids": ids,
                    "live": not s["closed_at"]})
    return out


def clean_candidates(sessions, tasks):
    """Every closed session (whether or not it owns work), with its non-done
    task ids."""
    return [{"name": s["name"], "task_ids": _nondone_ids(tasks, s["name"])}
            for s in sorted(sessions, key=lambda r: r["name"])
            if s["closed_at"]]


def restore_plan_text(cands, spawn_arm: str, missing_workdirs=()) -> str:
    lines = ["RESTORE PLAN"]
    for c in cands:
        ids = " ".join(f"#{i}" for i in c["task_ids"])
        if not c["workdir"]:
            lines.append(f"  SKIP {c['name']} - no known workdir "
                         f"(use relay clean, or re-run relay in the dir)")
            continue
        if c["name"] in missing_workdirs:
            lines.append(f"  SKIP {c['name']} - workdir no longer exists: "
                         f"{c['workdir']}")
            continue
        zombie = "  [tab still open - old tab left as a zombie]" if c["live"] else ""
        lines.append(f"  restore {c['name']} ({c['role']}) in {c['workdir']} "
                     f"- {len(c['task_ids'])} task(s): {ids}{zombie}")
    if not cands:
        lines.append("  (nothing to restore)")
    if spawn_arm == "off":
        lines.append("  WARNING: spawn_arm is off - restored workers will not "
                     "act unattended (arm them, or set [swarm] spawn_arm)")
    return "\n".join(lines)


def clean_plan_text(cands) -> str:
    lines = ["CLEAN PLAN"]
    for c in cands:
        n = len(c["task_ids"])
        reset = f"reset {n} task(s) to todo, " if n else ""
        lines.append(f"  {reset}remove session {c['name']}")
    if len(lines) == 1:
        lines.append("  (nothing to clean)")
    return "\n".join(lines)


# --- wipe planning (pure; rows in, plans out) --------------------------------

def _all_ids(tasks, owner):
    return [t["id"] for t in tasks if t["owner"] == owner]


def wipe_candidates(sessions, tasks, names=None):
    """Closed sessions to delete outright (optionally filtered to `names`),
    each with ALL its owned task ids (any state - wipe removes done too)."""
    out = []
    for s in sorted(sessions, key=lambda r: r["name"]):
        if not s["closed_at"]:
            continue
        if names is not None and s["name"] not in names:
            continue
        out.append({"name": s["name"], "task_ids": _all_ids(tasks, s["name"]),
                    "workdir": s.get("workdir", "") if hasattr(s, "get")
                               else s["workdir"],
                    "worktree_repo": s.get("worktree_repo", "")
                               if hasattr(s, "get") else s["worktree_repo"]})
    return out


def wipe_blocker_warnings(cands, tasks):
    """Warn when a task being wiped is a blocker of a task that is NOT being
    wiped - that dependent may never unblock once its blocker is gone."""
    wiped = set()
    for c in cands:
        wiped.update(c["task_ids"])
    out = []
    for t in tasks:
        if t["id"] in wiped:
            continue
        for b in parse_blockers(t["blocked_by"]):
            if b in wiped:
                out.append(f"WARNING: #{b} is a blocker of #{t['id']} "
                           f"(not being wiped) - #{t['id']} may never unblock")
    return out


def wipe_plan_text(cands, project_all=None) -> str:
    if project_all is not None:
        nt, ns, nm = project_all
        return ("WIPE PLAN (whole project)\n"
                f"  delete {nt} task(s), {ns} session(s), {nm} message(s)")
    lines = ["WIPE PLAN"]
    for c in cands:
        lines.append(f"  delete {len(c['task_ids'])} task(s), "
                     f"session {c['name']}")
        wa = c.get("worktree_action")
        if wa == "remove":
            lines.append(f"    remove worktree {c['workdir']} "
                         f"+ branch relay/{c['name']}")
        elif wa == "keep-dirty":
            lines.append(f"    KEEP worktree {c['workdir']} - uncommitted "
                         f"changes (relay never deletes unsaved work)")
    if len(lines) == 1:
        lines.append("  (nothing to wipe)")
    return "\n".join(lines)


def resume_prompt(name: str, project: str, role: str, spawn_prompt: str) -> str:
    skill = "relay-worker" if role == "worker" else "relay-coordinator"
    p = (f"Invoke the {skill} skill. You are '{name}'"
         + (f" on project '{project}'" if project else "")
         + ", RESUMING work a previous session left unfinished. Run "
         f"`relay task list --mine` and `relay inbox`, then continue the "
         f"in-progress task(s) from where they were left.")
    if spawn_prompt:
        p += f" Original mission: {spawn_prompt}"
    return p


# --- swarm view rendering (Rich markup; ALL dynamic text escaped) -------------

_STATE_COLS = ("todo", "doing", "blocked", "done")
_KIND_COLOR = {"done": "green", "blocked": "yellow",
               "escalation": "red", "wake": "dim"}
_MODE_GLYPH = {"safe": "◉", "wild": "▲", "insane": "✦"}


def _clip(s: str, w: int) -> str:
    s = str(s)
    return s if len(s) <= w else s[: max(0, w - 1)] + "…"


def _esc(s) -> str:
    """Escape for Rich markup: a literal [ in dynamic text (bodies, titles,
    names - attacker-influenceable) must never open a tag."""
    return str(s).replace("[", "\\[")


def _get(row, key, default=None):
    """Tolerant field access for sqlite Rows and plain dict fixtures alike."""
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        return default


def fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fleet_line(sessions, tasks, stale=frozenset(), queued: int = 0) -> str:
    """The one-line 'how many workers doing what' header. busy = owns a doing
    task, blocked = owns a blocked one (and nothing doing), idle = the rest;
    armed counts come from the persisted per-session mode."""
    names = [s["name"] for s in sessions]
    doing = {t["owner"] for t in tasks if t["state"] == "doing" and t["owner"]}
    blocked = {t["owner"] for t in tasks
               if t["state"] == "blocked" and t["owner"]}
    n_busy = sum(1 for n in names if n in doing)
    n_blocked = sum(1 for n in names if n in blocked and n not in doing)
    n_idle = len(names) - n_busy - n_blocked
    bits = [f"{len(names)} units", f"{n_busy} busy",
            f"{n_blocked} blocked", f"{n_idle} idle"]
    armed = {}
    for s in sessions:
        m = _get(s, "mode", "") or ""
        if m in _MODE_GLYPH:
            armed[m] = armed.get(m, 0) + 1
    if armed:
        bits.append("armed " + " ".join(
            f"{_MODE_GLYPH[m]}{armed[m]}"
            for m in ("safe", "wild", "insane") if m in armed))
    n_stale = sum(1 for n in names if n in stale)
    if n_stale:
        bits.append(f"{n_stale} STALE")
    if queued:
        bits.append(f"msgs {queued} queued")
    return "FLEET  " + " · ".join(bits)


def interaction_rows(messages, coordinators=frozenset(), now: float = 0.0,
                     limit: int = 6) -> list:
    """Who talks to whom: one row per unordered name pair - direction counts
    (from the first name's perspective; a coordinator is always listed
    first), last kind, age of the last message, and a flag when that last
    word was blocked/escalation. relay's own wake-ups are system noise, not
    interaction - excluded. Freshest pairs first, capped at `limit`."""
    pairs = {}
    for m in messages:
        a, b = m["from_name"], m["to_name"]
        if a == "relay" or b == "relay":
            continue
        key = tuple(sorted((a, b)))
        p = pairs.setdefault(key, {"sent": {}, "last_ts": -1.0,
                                   "last_kind": "info"})
        p["sent"][a] = p["sent"].get(a, 0) + 1
        ts = float(_get(m, "created_at", 0.0) or 0.0)
        if ts >= p["last_ts"]:
            p["last_ts"] = ts
            p["last_kind"] = kind_of(m)
    out = []
    for (x, y), p in pairs.items():
        a, b = ((y, x) if y in coordinators and x not in coordinators
                else (x, y))
        out.append({"a": a, "b": b,
                    "sent": p["sent"].get(a, 0), "recv": p["sent"].get(b, 0),
                    "last_kind": p["last_kind"],
                    "age_s": max(0.0, now - p["last_ts"]),
                    "flag": p["last_kind"] in ("blocked", "escalation")})
    out.sort(key=lambda r: r["age_s"])
    return out[:limit]


def progress_bar(done: int, total: int, cells: int = 10) -> str:
    if total <= 0:
        return "▱" * cells
    filled = min(cells, max(0, round(cells * done / total)))
    return "▰" * filled + "▱" * (cells - filled)


def render_swarm(sessions, tasks, messages, now: float, width: int = 100,
                 stale=frozenset(), activity=None) -> str:
    """One Rich-markup screen: fleet line, roster (heartbeats, stale marks),
    kanban board, epic progress bars, interaction map, kind-colored message
    feed. Grouped by project when more than one is present. With no swarm at
    all, teaches how to start one instead of rendering an empty skeleton.
    Callers render with markup=True; every dynamic string goes through
    _esc()."""
    activity = activity or {}
    if not sessions and not tasks:
        return (
            "NO SWARM YET\n"
            "\n"
            "This view shows named Claude sessions coordinating through relay:\n"
            "who is registered, a task board, and the message feed between them.\n"
            "\n"
            "Start one by spawning an armed worker:\n"
            "\n"
            "    relay spawn --name w1 --arm wild \"your task\"\n"
            "\n"
            "or, from a session you want in the swarm, register it:\n"
            "\n"
            "    relay register --name w1 --role worker --project myproj\n"
            "\n"
            "TAB returns to the session control view.")
    out: List[str] = []
    queued = sum(1 for m in messages if _get(m, "delivered_at") is None)
    out.append(_esc(fleet_line(sessions, tasks, stale=stale, queued=queued)))
    out.append("")
    projects = sorted({s["project"] for s in sessions}
                      | {t["project"] for t in tasks}) or [""]
    for proj in projects:
        p_sessions = [s for s in sessions if s["project"] == proj]
        p_tasks = [t for t in tasks if t["project"] == proj]
        coord = next((s["name"] for s in p_sessions
                      if s["role"] == "coordinator"), "-")
        workers = sum(1 for s in p_sessions if s["role"] == "worker")
        out.append(_esc(f"PROJECT {proj or '(none)'} · coordinator: {coord} · "
                        f"{workers} workers"))
        for s in p_sessions:
            hb = (f"  ↻ {fmt_age(now - activity[s['name']])}"
                  if s["name"] in activity else "")
            line = (f"  {s['name']:<16} {s['role']:<12} "
                    f"{_clip(_get(s, 'status_text', '') or '-', width - 40)}"
                    f"{hb}")
            if s["name"] in stale:
                out.append(f"[red]{_esc(line + ' ⧗')}[/red]")
            else:
                out.append(_esc(line))
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
            out.append(_esc("   ".join(
                (cols[st][i] if i < len(cols[st]) else "").ljust(colw)
                for st in _STATE_COLS)))
        out.append("")

        # epic progress: children done/total as a bar
        epics = [t for t in p_tasks if t["parent_id"] is None]
        for e in epics:
            kids = [t for t in p_tasks if t["parent_id"] == e["id"]]
            if kids:
                done = sum(1 for k in kids if k["state"] == "done")
                out.append(_esc(
                    f"  EPIC #{e['id']} {_clip(e['title'], width - 30)}"
                    f"  {progress_bar(done, len(kids))}  {done}/{len(kids)}"))
        out.append("")

    coords = {s["name"] for s in sessions if s["role"] == "coordinator"}
    inter = interaction_rows(messages, coordinators=coords, now=now)
    if inter:
        out.append("INTERACTIONS                    sent recv  last        age")
        for r in inter:
            flag = "  ‼" if r["flag"] else ""
            pair = _clip(f"{r['a']} ⇄ {r['b']}", 28)
            line = (f"  {pair:<28} ▸{r['sent']:<3} ◂{r['recv']:<3} "
                    f"{r['last_kind']:<10} {fmt_age(r['age_s']):>4}{flag}")
            color = _KIND_COLOR.get(r["last_kind"])
            out.append(f"[{color}]{_esc(line)}[/{color}]"
                       if r["flag"] and color else _esc(line))
        out.append("")

    out.append("MESSAGES")
    for m in messages[-8:]:
        q = "" if _get(m, "delivered_at") else "  [queued]"
        k = kind_of(m)
        tag = f"[{k}] " if k != "info" else ""
        line = (f"  {m['from_name']} -> {m['to_name']}: "
                f"{tag}{_clip(m['body'], width - 30)}{q}")
        color = _KIND_COLOR.get(k)
        out.append(f"[{color}]{_esc(line)}[/{color}]" if color else _esc(line))
    if not messages:
        out.append("  (none)")
    return "\n".join(out)
