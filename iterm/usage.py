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
from typing import Optional

# Every model relay is likely to watch ships a 200k context window. The 1M
# variants report the SAME model string (a session observed at 303,591 tokens
# reported plain "claude-opus-5"), so the window cannot be read off the
# transcript and is inferred instead: once a session is seen above 200k it is
# demonstrably not a 200k model. Self-correcting and never overstates - the
# worst case is one turn shown against the smaller window before the first
# reading above it arrives, and a percentage that reads high is the safe
# direction to be wrong in.
_BASE_WINDOW = 200_000
_WIDE_WINDOW = 1_000_000


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


def _blank(path: str) -> dict:
    return {"path": path, "offset": 0, "out": 0, "in": 0, "cached": 0,
            "turns": 0, "ctx": 0, "model": "", "window": _BASE_WINDOW}


def read(claude_session_id: str) -> Optional[dict]:
    """Current usage for a session, or None when there is nothing to read.

    Returns a dict with:
      ctx     - tokens in the model's context as of the LAST turn. This is the
                number the operator acts on, and it is a level, not a sum:
                prompt tokens for one turn, which is what a compaction resets.
      window  - the inferred context window (see _BASE_WINDOW).
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
                if st["ctx"] > st["window"]:
                    st["window"] = _WIDE_WINDOW
    except OSError:
        return None
    _STATE[path] = st
    if not st["turns"]:
        return None
    pct = min(100, int(round(100.0 * st["ctx"] / max(1, st["window"]))))
    return {"ctx": st["ctx"], "window": st["window"], "pct": pct,
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
    """The roster's CTX column: a bare percentage, or '' when there is nothing
    honest to show. Empty, NOT a zero or a dash-with-a-number: an unregistered
    tab has no transcript to read and must not render as a session using no
    context."""
    if not u:
        return ""
    return f"{u['pct']}%"


# Percentage at which the cell starts warning. Not a hard limit - Claude Code
# compacts on its own - but the point past which the operator may want to
# finish a thought rather than start one.
CTX_WARN = 75
CTX_HIGH = 90


def ctx_level(u: Optional[dict]) -> str:
    """'', 'ok', 'warn' or 'high' - the color band for the CTX cell. Kept
    separate from the text so the palette stays in app.py with every other
    color decision."""
    if not u:
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

    THREE distinct no-number states, because they need three different actions
    from the operator and collapsing them is how a working feature reads as a
    broken one:

    - not registered at all -> `relay join` (nothing to tie to)
    - registered, but with no session id on file -> ALSO `relay join`, but for
      a different reason: the session registered before relay recorded ids, so
      the row predates the column. This is every session in an existing swarm
      the first time it runs a build with usage in it, and reporting it as "no
      transcript yet" (as this did on first release) tells the operator to wait
      for something that will never arrive on its own.
    - registered with an id, but no transcript yet -> genuinely just wait; the
      session has not taken a turn.
    """
    if not registered:
        return ["TOKENS  not registered - relay cannot tie this tab",
                "        to a Claude transcript. `relay join` here",
                "        to enable it."]
    if not has_id:
        return ["TOKENS  registered before relay recorded session ids.",
                "        Run `relay join` in this tab once to enable it",
                "        (it keeps the name, mode and task)."]
    if not u:
        return ["TOKENS  no transcript yet - this session has not",
                "        taken a turn."]
    return [
        f"TOKENS  {u['pct']}% of context · {fmt_tokens(u['ctx'])}"
        f"/{fmt_tokens(u['window'])}",
        f"        out {fmt_tokens(u['out'])} · in {fmt_tokens(u['in'])}"
        f" · cached {fmt_tokens(u['cached'])}",
        f"        {u['turns']} turns"
        + (f" · {u['model']}" if u["model"] else ""),
    ]
