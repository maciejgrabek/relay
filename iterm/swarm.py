"""Relay swarm - pure decision logic (no iTerm2, no sqlite imports).

Like gates.py, this is the load-bearing logic kept pure so it can be
unit-tested: which tasks a completion unblocks, what wake-up messages say,
whether a screen tail is Claude's idle input box (safe to inject into), and
when a session counts as stale. Rows come in as dicts/sqlite Rows; both
support [] access.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

# Mirrors db.RESERVED_NAMES. Duplicated rather than imported so this module
# stays dependency-free (it is unit-tested standalone, like gates.py).
# db.register enforces the real rule; this only stops us ever PROPOSING a name
# that would be refused - which matters because the relay repo is itself
# called 'relay'.
_RESERVED = ("relay", "human")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_MAX = 24


def derive_name(cwd: str, taken) -> str:
    """A session name for a session nobody named, from its working directory.

    The operator's mental model is "the tab in <repo>", so the basename is the
    least surprising handle. Collisions are the common case (three sessions in
    one repo), so dedupe with a numeric suffix rather than something opaque:
    a human has to read these in `relay who` and type them into `relay send`.
    """
    base = str(cwd or "").rstrip("/")
    base = _SLUG_RE.sub("-", os.path.basename(base).lower()).strip("-")
    base = base[:_NAME_MAX].rstrip("-") or "session"
    taken = set(taken or ())

    def free(n: str) -> bool:
        return n not in taken and n not in _RESERVED

    if free(base):
        return base
    n = 2
    while not free(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


# --- working copies -------------------------------------------------------------
#
# Two sessions editing one checkout clobber each other, and relay knows every
# session's workdir - so this is a fact it can check rather than a rule it has
# to teach and hope was read. Kept here (pure) so the CLI's refusal and the TUI
# and doctor's reporting all read the same definition of "the same directory".

def real_workdir(p) -> str:
    """A workdir normalized for comparison: symlinks resolved, trailing slash
    dropped. /tmp vs /private/tmp is the same checkout on macOS, and reading it
    as two would silently disarm every check built on this.

    This duplicates db._norm_workdir's normalization rather than calling it:
    swarm.py is deliberately dependency-free (no db import, unit-tested
    standalone), and same_checkout/checkout_occupants need this before a
    session ever opens sqlite. Keep the two in agreement or callers that
    assume they are interchangeable get burned - in particular, the empty
    check MUST run before any trailing-slash strip: stripping "/" down to ""
    would silently turn a literal root path into "unknown workdir", which
    same_checkout treats as "never matches"."""
    s = str(p or "")
    if not s:
        return ""
    try:
        real = os.path.realpath(s) or s
    except Exception:
        real = s
    return real if real == "/" else real.rstrip("/")


def same_checkout(a, b) -> bool:
    """Whether two recorded workdirs are one working copy.

    An unknown (empty) workdir never matches: relay REFUSES work on the back of
    this, and manufacturing a conflict out of missing data would block a spawn
    that is fine."""
    ra, rb = real_workdir(a), real_workdir(b)
    return bool(ra) and ra == rb


def checkout_occupants(sessions, workdir, *, exclude=(), roles=("worker",)) -> List[str]:
    """Live sessions already working in `workdir`.

    Filtered to workers by default, because the common and correct setup is a
    coordinator sitting in the repo it delegates from: it writes specs and
    messages, not code. Two WORKERS in one checkout is the collision worth
    refusing."""
    ex = set(exclude or ())
    out = []
    for s in sessions:
        if _get(s, "closed_at", 0):
            continue
        name = _get(s, "name", "")
        if name in ex:
            continue
        if roles and _get(s, "role", "") not in roles:
            continue
        if same_checkout(_get(s, "workdir", ""), workdir):
            out.append(name)
    return out


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


_DELIVERY_MAX = 700


def _flatten(s: str) -> str:
    """Flatten to one line, drop anything that isn't printable, and bound the
    length. Injected text is raw keystrokes, so a stray ESC would be
    interpreted by the terminal rather than typed. (Distinct from _clip below,
    which is a display-width truncator for the TUI.)"""
    flat = " ".join(str(s).splitlines())
    flat = "".join(c for c in flat if c.isprintable() or c == " ")
    return flat[:_DELIVERY_MAX]


def batch_delivery_text(msgs) -> str:
    """The literal text typed into a session for its whole queued batch.

    One injected turn per batch, not per message: every delivery costs the
    recipient a full Claude turn, so three queued messages used to cost three
    turns to convey what one turn can carry. Still ONE line and one Enter -
    the bracketed-paste constraint has not changed - so a batch degrades to a
    count plus a pointer rather than trying to inline everything.
    """
    msgs = list(msgs or ())
    if not msgs:
        return ""
    # A batch that is ENTIRELY one discussion delivers a POINTER, not the
    # payload. The transcript arrives as bash output from `relay thread`, where
    # it can be multi-line and unabridged; injected text is one flattened line,
    # and a three-way transcript flattened onto one line is unreadable. Mixed
    # traffic falls through to the generic inbox pointer below, or the plain
    # messages would be silently invisible.
    tids = {_get(m, "thread_id", None) for m in msgs}
    if len(tids) == 1 and None not in tids:
        tid = tids.pop()
        senders = []
        for m in msgs:
            s = _get(m, "from_name", "")
            if s not in senders:
                senders.append(s)
        return _flatten(f"[relay discussion #{tid}] {len(msgs)} new post(s) "
                        f"from {', '.join(senders)} - read them first: "
                        f"relay thread {tid}")
    if len(msgs) == 1:
        m = msgs[0]
        base = delivery_text(_get(m, "from_name", ""), _get(m, "body", ""),
                             kind_of(m))
        return _flatten(f'{base}  (reply: relay reply {_get(m, "id", "")} '
                        f'"<your answer>")')
    senders = []
    for m in msgs:
        s = _get(m, "from_name", "")
        if s not in senders:
            senders.append(s)
    who = ", ".join(senders[:4]) + (", ..." if len(senders) > 4 else "")
    return _flatten(f"[relay {len(msgs)} messages from {who}] "
                    f"read them: relay inbox")


def kind_of(m) -> str:
    """A message row/dict's kind, defaulting 'info' for pre-v5 rows and plain
    dict fixtures (sqlite Row and dict both support .keys())."""
    try:
        k = m["kind"] if "kind" in m.keys() else ""
    except Exception:
        k = ""
    return k or "info"


# --- discussions: agreement is derived, never stored ---------------------------

def _ordered(msgs):
    return sorted(msgs, key=lambda x: (_get(x, "created_at", 0) or 0,
                                       _get(x, "id", 0) or 0))


def round_counts(msgs) -> dict:
    """`say` posts per participant.

    `agree` deliberately does not count: settling must never be rationed, or a
    session that spent its last round reaching a conclusion would be unable to
    record it."""
    out = {}
    for m in msgs:
        if kind_of(m) == "say":
            n = _get(m, "from_name", "")
            out[n] = out.get(n, 0) + 1
    return out


def positions(msgs) -> dict:
    """Each participant's LIVE position: its most recent `agree`, unless it has
    posted a `say` since.

    A session that is still talking is not settled. That is the whole
    retraction rule, and storing agreement in a column instead would have made
    it bookkeeping."""
    out = {}
    for m in _ordered(msgs):
        k, who = kind_of(m), _get(m, "from_name", "")
        if k == "agree":
            out[who] = _get(m, "body", "")
        elif k == "say":
            out.pop(who, None)
    return out


def last_said(msgs) -> dict:
    """Each participant's most recent utterance of any kind - what an
    unresolved thread reports in place of an agreement."""
    out = {}
    for m in _ordered(msgs):
        if kind_of(m) in ("say", "agree"):
            out[_get(m, "from_name", "")] = _get(m, "body", "")
    return out


def thread_verdict(participants, msgs, rounds_cap: int = 0):
    """(state, outcome) for a discussion. 'open' means keep going.

    ONE automatic terminal state, and it is not a judgement: when every
    participant has a live `agree`, the discussion is over because the agents
    ended it, and relay is only reading what they did.

    Relay deliberately does NOT close a discussion for any other reason. An
    earlier version closed it `unresolved` once a round cap was spent and
    escalated to the operator - that was relay deciding the agents had failed
    and that a human should settle it. Neither is relay's call. The decision
    belongs to the agents: they settle it, or they end it themselves
    (db.close_thread via `relay close`), or they escalate it themselves.

    `rounds_cap` is accepted and ignored, kept so callers need not change."""
    parts = list(participants)
    if not parts:
        return "open", ""
    pos = positions(msgs)
    if all(p in pos for p in parts):
        return "agreed", " | ".join(f"{p}: {pos[p]}" for p in parts)
    return "open", ""


def escalation_pings(msgs, already: set) -> list:
    """Queued messages that should ping the human NOW: kind 'escalation' and
    not already pinged. Delivery still waits for the target's idle prompt;
    the ping must not."""
    return [m for m in msgs
            if kind_of(m) == "escalation" and m["id"] not in already]


def escalations_to_close(pinged) -> list:
    """Message ids that the ping itself has fully handled: those addressed to
    'human'. Nothing injects into the operator's mailbox, so an unmarked one
    would sit undelivered forever - never pruned (prune_messages keeps queued
    rows on purpose) and re-pinged on every relay restart, since the pinged
    set is in memory."""
    return [m["id"] for m in pinged if m["to_name"] == "human"]


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


def prompt_line_empty(lines: List[str]) -> bool:
    """True when Claude's input box row is visibly EMPTY - no operator draft.

    An extreme push types text and presses Enter; landing on a half-typed
    message would append to it and SUBMIT it. So the input row ("│ > ...")
    must exist in the ready tail and carry nothing after the '>'. Scanned
    tail is wider than claude_prompt_ready's (6 lines, not 3): a two-line
    footer (both "? for shortcuts" and a "⏵⏵ accept edits" line, per
    _READY_MARKERS) pushes the input row further from the bottom, and a
    narrower scan would never find it - silently vetoing every push. No
    input row found => not a known-empty box => False (fail safe: no push)."""
    tail = [l for l in lines if l.strip()][-6:]
    for l in reversed(tail):
        if _INPUT_BOX_RE.match(l):
            rest = _INPUT_BOX_RE.sub("", l, count=1)
            return rest.strip("".join(_BOX_GLYPHS) + " \t") == ""
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


# --- PR reference parsing and routing ----------------------------------------

# owner/name#number - the single PR reference format, used by every verb and
# every message. One format is one less thing for a session to get wrong.
_PR_REF_RE = re.compile(r"^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)$")


def parse_pr_ref(ref: str):
    """('acme/api', 482) or None. Strict: a caller that guesses at the format
    gets an error, not a row written under a ref nothing will ever match.

    The repo half is lowercased: GitHub repo names are case-insensitive
    (Acme/API and acme/api are the same repo), but idx_prs_ref is a plain
    case-sensitive TEXT index, so without normalizing here the same PR could
    land in two rows under different casing - one claimed, the other queried,
    each blind to the other. Every write and lookup goes through this
    function, so normalizing here is enough. The PR number is untouched."""
    m = _PR_REF_RE.match((ref or "").strip())
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2))


def resolve_pr_route(pr, session):
    """Can PR feedback be routed to the session that opened it?

    Returns (status, detail): ('ok', owner_name) when the claiming session is
    still the session behind that name; ('unclaimed', '') when nobody claimed
    the PR; ('gone', reason) otherwise.

    The identity check is the point. Relay names are reclaimable - re-register
    rebinds a name to a new tab, and `relay restore` depends on that - so
    routing on the name alone would deliver 'your PR has changes requested' to
    a tab that has never seen the branch. Comparing the iTerm session id
    recorded at claim time makes 'is this still the author?' answerable."""
    if pr is None or not (_get(pr, "owner", "") or ""):
        return ("unclaimed", "")
    owner = pr["owner"]
    if session is None:
        return ("gone", f"no session registered as '{owner}'")
    if _get(session, "closed_at", 0):
        return ("gone", f"session '{owner}' closed")
    if _get(session, "iterm_session_id", "") != _get(pr, "owner_session_id",
                                                     ""):
        return ("gone", f"'{owner}' was rebound to a different tab since the "
                        f"PR was claimed")
    return ("ok", owner)


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


def worktree_removals(sessions, exists_fn, dirty_fn):
    """The relay-created worktrees to act on when wiping a whole project. Both
    wipe paths must clean these up - the per-session path already does, but the
    `--all` path historically deleted only DB rows, orphaning the worktrees (and
    their relay/<name> branches) on disk. Returns [{name, repo, workdir,
    action}] where action is 'remove' (clean) or 'keep-dirty' (uncommitted work
    - NEVER destroyed, matching the per-session safety). Sessions with no relay
    worktree, or whose workdir is already gone, are skipped. exists_fn/dirty_fn
    are injected (os.path.isdir / a git status check) so this stays pure."""
    def g(s, k):
        return (s.get(k, "") if hasattr(s, "get") else s[k]) or ""
    out = []
    for s in sorted(sessions, key=lambda r: g(r, "name")):
        repo, workdir = g(s, "worktree_repo"), g(s, "workdir")
        if not (repo and workdir and exists_fn(workdir)):
            continue
        out.append({"name": g(s, "name"), "repo": repo, "workdir": workdir,
                    "action": "keep-dirty" if dirty_fn(workdir) else "remove"})
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
        mc = c.get("msg_count")
        msg = f"{mc} message(s), " if mc is not None else ""
        lines.append(f"  delete {len(c['task_ids'])} task(s), {msg}"
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
_MODE_GLYPH = {"safe": "◉", "wild": "▲", "insane": "✦", "extreme": "✷"}


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


def fleet_line(sessions, tasks, stale=frozenset(), queued: int = 0,
              prs=()) -> str:
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
            for m in ("safe", "wild", "insane", "extreme") if m in armed))
    n_stale = sum(1 for n in names if n in stale)
    if n_stale:
        bits.append(f"{n_stale} STALE")
    if queued:
        bits.append(f"msgs {queued} queued")
    if prs:
        need = sum(1 for r in prs if r["flag"])
        bits.append(f"PRs {len(prs)}" + (f" · {need} need work" if need else ""))
    return "FLEET  " + " · ".join(bits)


# The fleet map: one cell per unit in offset rows, no names. It is for
# peripheral vision - "is anything hot?" - and the roster right below it is
# where names live. Under this many units the roster already answers that at
# a glance, so the map would be three rows saying nothing.
FLEET_MAP_MIN_UNITS = 8
FLEET_MAP_PER_ROW = 12
# The honeycomb is the OFFSET, not the glyph. ⬢/⬡ (U+2B22/U+2B21) would be the
# literal choice but they are absent from common terminal fonts - Hack Nerd
# Font Mono has neither - and a fallback glyph from another font breaks the
# monospace grid the map depends on. ●/○ are present essentially everywhere
# and read the same at a glance: filled = live, hollow = idle.
FLEET_MAP_LIVE, FLEET_MAP_IDLE = "●", "○"


def _unit_cell(name, stale, blocked, doing, mode) -> str:
    """One unit's cell: filled when it is doing something or armed, hollow
    when it is idle. Color is urgency, in the swarm view's existing
    vocabulary - red is the stale mark, yellow is the blocked kind, green is
    live work. Loudest state wins: a unit that needs a human is never drawn
    as merely busy."""
    live = FLEET_MAP_LIVE
    if name in stale:
        return f"[red]{live}[/red]"
    if name in blocked:
        return f"[yellow]{live}[/yellow]"
    if name in doing:
        return f"[green]{live}[/green]"
    if mode in _MODE_GLYPH:
        return live
    return f"[dim]{FLEET_MAP_IDLE}[/dim]"


def fleet_map(sessions, tasks, stale=frozenset(), width: int = 100) -> list:
    """Rows of cells, one per unit, offset like a honeycomb so a hot one
    breaks the grid instead of hiding in a column. Empty for a small fleet."""
    if len(sessions) < FLEET_MAP_MIN_UNITS:
        return []
    doing = {t["owner"] for t in tasks if t["state"] == "doing" and t["owner"]}
    blocked = ({t["owner"] for t in tasks
                if t["state"] == "blocked" and t["owner"]} - doing)
    per_row = max(4, min(FLEET_MAP_PER_ROW, (width - 6) // 3))
    cells = [_unit_cell(s["name"], stale, blocked, doing, _get(s, "mode", ""))
             for s in sessions]
    rows = []
    for i in range(0, len(cells), per_row):
        indent = "   " if (i // per_row) % 2 else "  "
        rows.append(indent + "  ".join(cells[i:i + per_row]))
    return rows


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


# --- PR pane -------------------------------------------------------------

# State glyphs share the vocabulary the rest of the swarm view already uses:
# ⊘ is the blocked/needs-work mark, ✓ is done, ◷ is waiting.
_PR_GLYPH = {"created": "◦", "review": "◷", "changes": "⊘",
             "approved": "✓", "merged": "●", "closed": "✕"}
_PR_COLOR = {"changes": "yellow", "approved": "green", "merged": "dim",
             "closed": "dim"}

# States that mean a human or a worker still owes this PR something.
_PR_NEEDS_WORK = ("changes",)


def pr_rows(prs, sessions, now: float) -> list:
    """One display row per PR, in the order given (list_prs already returns
    the stable repo/number order). `flag` marks the rows that need attention:
    changes requested, nobody claimed it, or the claiming session is gone."""
    by_name = {s["name"]: s for s in sessions}
    out = []
    for p in prs:
        status, _ = resolve_pr_route(p, by_name.get(_get(p, "owner", "")))
        if status == "ok":
            label, gone = p["owner"], False
        elif status == "unclaimed":
            label, gone = "UNCLAIMED", True
        else:
            label, gone = f"{p['owner']} GONE", True
        state = _get(p, "state", "created")
        out.append({
            "ref": f"{p['repo']}#{p['number']}",
            "state": state,
            "age_s": max(0.0, now - float(_get(p, "state_changed_at", 0) or 0)),
            "owner_label": label,
            "task_id": _get(p, "task_id"),
            "flag": bool(gone or state in _PR_NEEDS_WORK),
        })
    return out


def _pr_line(r, width: int, mark: str = " ") -> str:
    """Age always rides beside the state: relay only knows what a session last
    told it, and a bare 'approved' would read as fact. `mark` is how the same
    row renders in the attention strip, so the strip and the list cannot drift
    apart in formatting. Column widths scale with `width` so the pane doesn't
    run off a narrow terminal or look starved on a wide one."""
    refw = max(10, min(22, width - 40))
    ownerw = max(10, min(18, width - 46))
    glyph = _PR_GLYPH.get(r["state"], "·")
    task = f"  #{r['task_id']}" if r["task_id"] else ""
    line = (f" {mark} {_clip(r['ref'], refw):<{refw}} {glyph}{r['state']:<9} "
            f"{fmt_age(r['age_s']):>4}  {_clip(r['owner_label'], ownerw)}{task}")
    color = ("red" if "UNCLAIMED" in r["owner_label"]
             or "GONE" in r["owner_label"]
             else _PR_COLOR.get(r["state"]))
    return f"[{color}]{_esc(line)}[/{color}]" if color else _esc(line)


def render_prs(rows, width: int = 100) -> list:
    """Attention strip on top, then every PR in stable order below it. The
    main list never reorders as states change, so a row stays where the eye
    last found it; anything urgent is DUPLICATED above rather than moved.

    Always renders, like MESSAGES: a section that vanishes when empty reads
    as "relay has no such feature" rather than "nothing here yet", and the
    empty state is where the operator most needs telling how one gets filled."""
    if not rows:
        return ["PULL REQUESTS",
                "[dim]  (none - a session records one with: "
                "relay pr claim owner/name#482)[/dim]"]
    out = ["PULL REQUESTS"]
    flagged = [r for r in rows if r["flag"]]
    for r in flagged:
        out.append(_pr_line(r, width, mark="‼"))
    if flagged:
        out.append("  " + "─" * max(10, min(width - 4, 60)))
    for r in rows:
        out.append(_pr_line(r, width))
    return out


def thread_row(th, msgs, now: float) -> dict:
    """One discussion, reduced to what the panel shows. Pure: the caller does
    the DB work and hands over the thread plus its posts."""
    parts = [p for p in str(_get(th, "participants", "")).split(",") if p]
    pos = positions(msgs)
    state = _get(th, "state", "open")
    return {
        "id": _get(th, "id", 0),
        "topic": _get(th, "topic", ""),
        "state": state,
        "outcome": _get(th, "outcome", "") or "",
        "settled": len([p for p in parts if p in pos]),
        "total": len(parts),
        "age_s": max(0.0, now - (_get(th, "created_at", 0) or 0)),
        # A closed discussion is the operator's cue: it has a verdict nobody
        # has read yet. An open one is the sessions' business, not theirs.
        "flag": state != "open",
    }


def _discussion_line(r, width: int, mark: str = " ") -> str:
    head = f"  {mark} #{r['id']:<4} "
    tail = f" {r['settled']}/{r['total']} settled  {fmt_age(r['age_s']):>5}"
    if r["state"] != "open":
        tail = f" {r['state']}: {_clip(r['outcome'], 40)}"
    topic = _clip(r["topic"], max(12, width - len(head) - len(tail) - 2))
    line = f"{head}{topic}{tail}"
    return f"[yellow]{_esc(line)}[/yellow]" if r["flag"] else _esc(line)


def render_discussions(rows, width: int = 100) -> list:
    """Attention strip on top, then every discussion in stable id order below.

    Same contract as render_prs: the main list NEVER reorders as states change,
    so a row stays where the eye last found it, and anything needing the
    operator is DUPLICATED above rather than moved. Always renders, even empty
    - a section that vanishes reads as "relay has no such feature"."""
    if not rows:
        return ["DISCUSSIONS",
                "[dim]  (none - a session opens one with: "
                "relay discuss <name> <name> \"<question>\")[/dim]"]
    out = ["DISCUSSIONS"]
    flagged = [r for r in rows if r["flag"]]
    for r in flagged:
        out.append(_discussion_line(r, width, mark="‼"))
    if flagged:
        out.append("  " + "─" * max(10, min(width - 4, 60)))
    for r in sorted(rows, key=lambda x: x["id"]):
        out.append(_discussion_line(r, width))
    return out


def progress_bar(done: int, total: int, cells: int = 10) -> str:
    if total <= 0:
        return "▱" * cells
    filled = min(cells, max(0, round(cells * done / total)))
    return "▰" * filled + "▱" * (cells - filled)


def render_swarm(sessions, tasks, messages, now: float, width: int = 100,
                 stale=frozenset(), activity=None, prs=(), threads=()) -> str:
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
    prows = pr_rows(prs, sessions, now) if prs else []
    out.append(_esc(fleet_line(sessions, tasks, stale=stale, queued=queued,
                               prs=prows)))
    # The map is already markup (colors are its whole point) - it carries no
    # user text, so it does not go through _esc().
    fmap = fleet_map(sessions, tasks, stale=stale, width=width)
    if fmap:
        out.append("")
        out.extend(fmap)
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
        # A task that produced a PR carries it on its card - the kanban stays
        # relay's own state machine, the PR pane stays the authority on PR
        # state, and this is the one thread between them.
        pr_by_task = {p["task_id"]: p for p in prs
                      if _get(p, "task_id") is not None}
        def _card(t):
            # The suffix must never win at the title's expense: degrade it
            # (full -> "PR<n>" -> nothing) until the whole cell - head,
            # title, suffix together - fits colw. A task with no PR takes
            # the p-is-None branch, which reduces to the pre-PR formula
            # exactly (suffix contributes 0 either way) - byte-identical
            # output at every width.
            p = pr_by_task.get(t["id"])
            head = f"#{t['id']} "
            if p is None:
                return head + _clip(t["title"], max(4, colw - len(head)))
            full = (f" ▸ PR {p['number']} {_PR_GLYPH.get(p['state'], '')}"
                    f"{p['state']}")
            minimal = f" PR{p['number']}"
            for suffix in (full, minimal, ""):
                titlew = colw - len(head) - len(suffix)
                if titlew >= 4 or not suffix:
                    return head + _clip(t["title"], max(4, titlew)) + suffix
        cols = {st: [_card(t) for t in p_tasks if t["state"] == st]
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

    out.extend(render_prs(prows, width))
    out.append("")

    out.extend(render_discussions(list(threads), width))
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


def _park_context_dict(row) -> dict:
    """The parsed context stamp, or {} for absent/malformed JSON - shared by
    parked_item_text (what to print) and has_park_context (whether there is
    anything to print), so the two can never disagree about what counts as
    context."""
    import json
    raw = _get(row, "context", "") or ""
    try:
        ctx = json.loads(raw) if raw else {}
    except Exception:
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def has_park_context(row) -> bool:
    """Whether a parked row's context stamp has anything parked_item_text
    would actually render - i.e. whether telling the reader to "read the
    context above" would be pointing at real lines instead of nothing."""
    ctx = _park_context_dict(row)
    return any(ctx.get(k) for k in ("doing", "last", "status"))


def parked_item_text(row) -> str:
    """One parked item, rendered for a session that is about to work on it.

    The context stamp is what makes a seven-word idea decodable three days
    later, so it is printed, not summarised. A malformed stamp degrades to no
    context rather than an error: it is decoration on top of the title, and
    losing it must never cost the operator the item.

    Owner is only shown when the row has one. Unowned is the normal case for
    a freshly parked item and stays visually quiet; an owned item must say
    whose it is, because `relay parked` lists it right alongside items
    anyone can take, and without this a reader cannot tell "unclaimed" from
    "earmarked for someone else" - which is exactly why `relay next` just
    refused it.
    """
    tid = _get(row, "id", "?")
    out = [f"#{tid}  {_get(row, 'title', '')}",
           f"     dir  {_get(row, 'workdir', '')}"]
    owner = _get(row, "owner")
    if owner:
        out.append(f"   owner  {owner}")
    ctx = _park_context_dict(row)
    for key, label in (("doing", "doing"), ("last", "last"),
                       ("status", "status")):
        if ctx.get(key):
            out.append(f"  {label:>7}  {ctx[key]}")
    return "\n".join(out)
