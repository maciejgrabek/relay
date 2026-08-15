"""Token usage for the sessions relay watches.

Relay does not talk to Claude Code - it watches iTerm2 tabs and reads their
screens. So where does a token count come from?

A Claude Code process exports CLAUDE_CODE_SESSION_ID into its own environment,
and `relay join` / `relay register` run INSIDE that process. The CLI therefore
inherits the id and records it next to the ITERM_SESSION_ID it already stores.
That pair is an EXACT join, not a heuristic - which is the whole reason this is
worth having on a panel whose job is telling the operator the truth.

The id is a UUID and Claude Code names the transcript after it, so a single
glob finds the file wherever it lives:

    ~/.claude/projects/<any-project-dir>/<session-id>.jsonl

No cwd-to-slug derivation, and nothing breaks when a session changes directory
mid-run.

Deliberately NOT done: guessing an unregistered tab's transcript from its
workdir by picking the newest file in the matching project directory. Sibling
tabs in one directory are relay's normal case (the parked-work spec is built on
it), and both would resolve to the same transcript - so two sessions would show
one session's numbers. A blank cell is never a wrong number; a plausible wrong
one is worse than nothing here.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from typing import Optional

# The context window is NEVER in the transcript: the 1M variants report the
# same model string as the 200k ones (a session observed at 303,591 tokens
# reported plain "claude-opus-5"). So relay settles it one of three ways, and
# which one it used is reported alongside the number:
#
#   observed - a reading above 200k. Proof: that session is not a 200k model.
#   config   - the `[1m]` suffix survives in ~/.claude/settings.json, where the
#              operator chose it. A default, not a fact, but a named one.
#   assumed  - neither. 200k is used to compute a percentage, but the
#              percentage is NOT shown; see ctx_cell().
#
# The original build had only the first route and treated 200k as the default
# denominator. That is a 5x error on every 1M session below 200k, and it does
# not self-correct: such a session may never cross the line. A live one at
# 187,021 tokens (18.7% of its real window) rendered as 94% in danger red - a
# healthy session painted as about to die, which is exactly the kind of wrong
# number this module's opening argument says it will not print.
_BASE_WINDOW = 200_000
_WIDE_WINDOW = 1_000_000


def settings_path() -> str:
    """Claude Code's user settings file, where the chosen model is recorded.
    Overridable for tests like projects_root() / sessions_root()."""
    return os.environ.get(
        "RELAY_CLAUDE_SETTINGS",
        os.path.expanduser("~/.claude/settings.json"))


# {"mtime": float, "window": int} - the settings file is read on every panel
# tick otherwise, for a value that changes when the operator changes it.
_SETTINGS = {"mtime": -1.0, "window": 0}


def configured_window() -> int:
    """The window implied by the CONFIGURED model, or 0 when none is set.

    Claude Code strips the `[1m]` suffix from the transcript but keeps it in
    settings.json ("claude-opus-5[1m]"), and that suffix is the whole of the
    question: with it, the 1M variant; without it, the standard 200k one. The
    rule is the suffix, not a table of model names, so a model relay has never
    heard of still resolves correctly.

    Deliberately the GLOBAL settings file only. A project-level
    .claude/settings.json or an in-session `/model` is not seen - reading those
    would need the session's cwd threaded through here, and the observed route
    still overrules this one in the dangerous direction (a session that turns
    out to be bigger than configured is corrected the first time it proves it).
    The reverse - configured 1M, session switched down to 200k - understates
    the percentage until the operator fixes the setting, which is why the
    preview names this window as coming from settings rather than from a
    reading.
    """
    try:
        mtime = os.path.getmtime(settings_path())
    except OSError:
        _SETTINGS["mtime"], _SETTINGS["window"] = -1.0, 0
        return 0
    if mtime == _SETTINGS["mtime"]:
        return _SETTINGS["window"]
    window = 0
    try:
        with open(settings_path()) as fh:
            model = str((json.load(fh) or {}).get("model") or "")
        if model:
            window = _WIDE_WINDOW if model.endswith("[1m]") else _BASE_WINDOW
    except (OSError, ValueError, TypeError, AttributeError):
        window = 0
    _SETTINGS["mtime"], _SETTINGS["window"] = mtime, window
    return window


def sessions_root() -> str:
    """Where Claude Code records one JSON file per RUNNING session, named for
    its process id. Overridable for tests like projects_root()."""
    return os.environ.get(
        "RELAY_CLAUDE_SESSIONS",
        os.path.expanduser("~/.claude/sessions"))


# pid -> session id, and the sessions-dir mtime the map was built at. Rebuilt
# when the directory changes, so a session that starts (or exits) mid-run is
# picked up without a per-tick directory scan.
_PID_INDEX: dict = {}
_PID_INDEX_MTIME = [-1.0]
# tab foreground-job pid -> resolved session id (or '' for "walked and found
# nothing"). The walk costs a few `ps` calls; the panel asks per tab per tick.
_PID_WALK: dict = {}


def _sessions_index() -> dict:
    """{claude pid: session id} from ~/.claude/sessions/<pid>.json.

    Rebuilt only when the directory's mtime moves. Files are keyed by pid AND
    carry `pid` inside; the inner value is trusted because a stale filename
    (pid reused by an unrelated process after a crash) would otherwise bind a
    tab to a transcript that was never its own.
    """
    root = sessions_root()
    try:
        mtime = os.path.getmtime(root)
    except OSError:
        _PID_INDEX.clear()
        return _PID_INDEX
    if mtime == _PID_INDEX_MTIME[0]:
        return _PID_INDEX
    idx = {}
    try:
        for name in os.listdir(root):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name)) as fh:
                    d = json.load(fh)
                pid, sid = int(d.get("pid") or 0), str(d.get("sessionId") or "")
            except (OSError, ValueError, TypeError):
                continue
            if pid and sid:
                idx[pid] = sid
    except OSError:
        return _PID_INDEX
    _PID_INDEX.clear()
    _PID_INDEX.update(idx)
    _PID_INDEX_MTIME[0] = mtime
    _PID_WALK.clear()      # parents may have changed under the new index
    return _PID_INDEX


def _parent_pid(pid: int) -> int:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
        return int(out.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


MAX_WALK = 8


def session_id_for_pid(pid) -> str:
    """The Claude session id owning a tab's foreground job, or ''.

    iTerm2 reports the FOREGROUND job's pid, which for a Claude tab is often a
    descendant rather than claude itself - an MCP server, a running Bash tool,
    a spawned agent. (Measured on a live window: iTerm2 said 92157, which was
    `chrome-devtools-mcp`, whose grandparent 92030 was the actual `claude`.) So
    walk up the process tree until a pid appears in the sessions index.

    This is what makes usage work WITHOUT registration. `relay join` recording
    CLAUDE_CODE_SESSION_ID is still the explicit path, but it only covers
    sessions that registered, and it goes stale the moment a tab restarts
    Claude - the DB keeps pointing at the previous run's transcript. Reading
    the live process tree has neither problem, so it is preferred and the DB
    value is the fallback.

    Bounded to MAX_WALK hops so a deep tool tree cannot turn one tab's lookup
    into an unbounded ps chain, and cached per pid because the answer for a
    live process never changes.
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return ""
    if pid <= 1:
        return ""
    if pid in _PID_WALK:
        return _PID_WALK[pid]
    idx = _sessions_index()
    cur, hops = pid, 0
    while cur > 1 and hops < MAX_WALK:
        if cur in idx:
            _PID_WALK[pid] = idx[cur]
            return idx[cur]
        cur = _parent_pid(cur)
        hops += 1
    # Cached as a miss too: a shell tab or relay's own panel would otherwise
    # re-walk the whole tree every tick forever.
    _PID_WALK[pid] = ""
    return ""


def projects_root() -> str:
    """Where Claude Code keeps transcripts. Overridable for tests, matching
    the RELAY_DB / RELAY_CONFIG / RELAY_AUDIT_LOG idiom the rest of relay
    uses - read at call time, never cached at import, so a test that sets it
    after import is still honoured."""
    return os.environ.get(
        "RELAY_CLAUDE_PROJECTS",
        os.path.expanduser("~/.claude/projects"))


def transcript_path(claude_session_id: str) -> Optional[str]:
    """The transcript for a session id, or None.

    Globbed rather than derived: the project directory is a slug of the cwd
    Claude Code started in, and reconstructing that slug would be a second
    source of truth that drifts the moment the slug rules change. The id is a
    UUID, so one match is the only possible outcome.
    """
    if not claude_session_id:
        return None
    # A session id reaches this from the DB, where it was written by a CLI
    # process. Refuse anything with path syntax in it rather than letting it
    # escape the projects root through the glob.
    if "/" in claude_session_id or "\\" in claude_session_id \
            or claude_session_id.startswith("."):
        return None
    hit = _PATHS.get(claude_session_id)
    if hit and os.path.exists(hit):
        return hit
    hits = glob.glob(os.path.join(projects_root(), "*",
                                  f"{claude_session_id}.jsonl"))
    if not hits:
        # Deliberately NOT cached as a miss: the panel resolves this on every
        # refresh tick, and a session that registers before its first turn has
        # no transcript on disk yet. A negative cache would hide it forever.
        return None
    _PATHS[claude_session_id] = hits[0]
    return hits[0]


# session id -> resolved transcript path. The panel asks for every session on
# every refresh tick, and a glob across every project directory each time is
# filesystem work for an answer that effectively never changes.
_PATHS: dict = {}

# path -> {"offset": int, "out": int, "in": int, "cached": int, "turns": int,
#          "ctx": int, "model": str, "window": int}
# Module-level so repeated reads are incremental. A transcript reaches 4.7 MB
# and 1,400+ assistant messages in a day; re-parsing that on every refresh tick,
# for every session, would cost more than everything else the panel does.
_STATE: dict = {}


def reset_cache() -> None:
    """Drop all incremental state. For tests, and for a panel restart."""
    _STATE.clear()
    _PATHS.clear()
    _PID_INDEX.clear()
    _PID_INDEX_MTIME[0] = -1.0
    _PID_WALK.clear()
    _SETTINGS["mtime"], _SETTINGS["window"] = -1.0, 0


def _blank(path: str) -> dict:
    return {"path": path, "offset": 0, "out": 0, "in": 0, "cached": 0,
            "turns": 0, "ctx": 0, "model": "", "wide": False}


def read(claude_session_id: str) -> Optional[dict]:
    """Current usage for a session, or None when there is nothing to read.

    Returns a dict with:
      ctx     - tokens in the model's context as of the LAST turn. This is the
                number the operator acts on, and it is a level, not a sum:
                prompt tokens for one turn, which is what a compaction resets.
                It lags: the turn in flight, and every tool result since the
                last one completed, are not in it yet.
      window  - the context window (see _BASE_WINDOW for the three routes).
      window_source - 'observed', 'config' or 'assumed'.
      window_known  - False for 'assumed', where pct is a guess at a
                denominator and callers must not render it as a percentage.
      pct     - ctx as a percentage of window, clamped to 100.
      out/in  - cumulative output and real (uncached) input tokens.
      cached  - cumulative cache reads. Enormous by design and NOT added to the
                others: it is repeat billing on the same prompt, so a "total
                tokens" built from it would be a number that measures nothing.
      turns   - assistant messages seen.
      model   - the last model string on record.

    Incremental: only bytes appended since the previous call are parsed.
    """
    path = transcript_path(claude_session_id)
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    st = _STATE.get(path)
    if st is None or size < st["offset"]:
        # Smaller than where we stopped means the file was replaced or
        # truncated, so every byte we counted is gone - start over rather than
        # add new totals onto stale ones.
        st = _blank(path)
    try:
        with open(path, "r", errors="replace") as fh:
            fh.seek(st["offset"])
            for line in fh:
                if not line.endswith("\n"):
                    # A partial final line: Claude Code is mid-write. Stop
                    # BEFORE it and leave the offset short, so the whole line
                    # is read once it is complete instead of being parsed in
                    # half and skipped forever.
                    break
                st["offset"] += len(line.encode("utf-8", "replace"))
                try:
                    d = json.loads(line)
                except (ValueError, TypeError):
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                st["turns"] += 1
                st["out"] += int(u.get("output_tokens") or 0)
                st["in"] += int(u.get("input_tokens") or 0)
                st["cached"] += int(u.get("cache_read_input_tokens") or 0)
                # The context level for THIS turn: everything that was in the
                # prompt, cached or not. Overwritten each turn rather than
                # summed - it is a level, and summing it would grow without
                # bound while the real context sat still.
                st["ctx"] = (int(u.get("input_tokens") or 0)
                             + int(u.get("cache_read_input_tokens") or 0)
                             + int(u.get("cache_creation_input_tokens") or 0))
                if msg.get("model"):
                    st["model"] = str(msg["model"])
                if st["ctx"] > _BASE_WINDOW:
                    # Proof, and it sticks: a compaction drops the level back
                    # under 200k but does not turn the model into a 200k one.
                    st["wide"] = True
    except OSError:
        return None
    _STATE[path] = st
    if not st["turns"]:
        return None
    # Resolved per read rather than frozen at first sight, so editing
    # settings.json is picked up by a running panel. A reading always beats the
    # settings file: it is the only route that observed THIS session.
    if st["wide"]:
        window, source = _WIDE_WINDOW, "observed"
    else:
        cfg = configured_window()
        window, source = (cfg, "config") if cfg else (_BASE_WINDOW, "assumed")
    pct = min(100, int(round(100.0 * st["ctx"] / max(1, window))))
    return {"ctx": st["ctx"], "window": window, "pct": pct,
            "window_source": source, "window_known": source != "assumed",
            "out": st["out"], "in": st["in"], "cached": st["cached"],
            "turns": st["turns"], "model": st["model"]}


def fmt_tokens(n: int) -> str:
    """Compact token count: 812, 48.2k, 1.2M. Terminal columns are scarce and
    an exact 1,203,441 costs eight cells to say what 1.2M says in four."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def ctx_cell(u: Optional[dict]) -> str:
    """The roster's CTX column. Three states, because relay knows three
    different amounts about a tab:

      ''      - nothing to show. Empty, NOT a zero or a dash-with-a-number: a
                tab relay cannot tie to a transcript is not a session at 0%.
      '48.1k' - the level, when the window is only assumed. A percentage here
                would be a denominator relay invented; the level is a number it
                read off disk. Costs the at-a-glance comparison and keeps the
                cell true, which is the trade this panel exists to make.
      '62%'   - the window was observed or configured, so the fraction means
                something.
    """
    if not u:
        return ""
    if not u.get("window_known"):
        return fmt_tokens(u.get("ctx", 0))
    return f"{u['pct']}%"


# Percentage at which the cell starts warning. Not a hard limit - Claude Code
# compacts on its own - but the point past which the operator may want to
# finish a thought rather than start one.
CTX_WARN = 75
CTX_HIGH = 90


def ctx_level(u: Optional[dict]) -> str:
    """'', 'ok', 'warn' or 'high' - the color band for the CTX cell. Kept
    separate from the text so the palette stays in app.py with every other
    color decision.

    No band without a known window: a threshold needs a denominator, and a red
    cell that turns out to be a session at 18% is how an operator learns to
    stop reading the column. Note this can only ever suppress a band below
    200k - past that the window is observed, so a genuinely full session still
    goes red.
    """
    if not u or not u.get("window_known"):
        return ""
    if u["pct"] >= CTX_HIGH:
        return "high"
    if u["pct"] >= CTX_WARN:
        return "warn"
    return "ok"


def preview_lines(u: Optional[dict], registered: bool,
                  has_id: bool = True) -> list:
    """The preview pane's TOKENS block, as plain text lines (that pane renders
    with markup OFF, so no color tags here).

    Two no-number states, and they need different actions from the operator -
    collapsing them is how a working feature reads as a broken one:

    - no session id found by ANY route -> almost always "Claude is not running
      in this tab". Registration is no longer the fix, because the process-tree
      lookup needs none; `relay join` survives only as the explicit fallback
      for when ~/.claude/sessions is unavailable.
    - id found, but no transcript yet -> genuinely just wait; the session has
      not taken a turn.

    `registered` is kept in the signature and no longer branches on its own:
    an unregistered tab now reports usage exactly like a registered one, and
    telling it to register would be advice that changes nothing.
    """
    if not has_id:
        return ["TOKENS  relay could not tie this tab to a Claude session.",
                "        Usually means Claude is not running here. If it is,",
                "        `relay join` in this tab records the link directly."]
    if not u:
        return ["TOKENS  no transcript yet - this session has not",
                "        taken a turn."]
    if not u.get("window_known"):
        # The roster cell had room for the level and nothing else. This is
        # where the operator finds out why there is no percentage, and that it
        # is one line of config away from being one.
        head = [f"TOKENS  {fmt_tokens(u['ctx'])} in context · window unknown",
                "        a 1M model reports the same name as a 200k one - set",
                "        `model` in ~/.claude/settings.json for a percentage."]
    else:
        head = [f"TOKENS  {u['pct']}% of context · {fmt_tokens(u['ctx'])}"
                f"/{fmt_tokens(u['window'])}"
                + (" · window from settings"
                   if u.get("window_source") == "config" else "")]
    return head + [
        f"        out {fmt_tokens(u['out'])} · in {fmt_tokens(u['in'])}"
        f" · cached {fmt_tokens(u['cached'])}",
        f"        {u['turns']} turns"
        + (f" · {u['model']}" if u["model"] else "")
        + " · as of the last turn",
    ]
