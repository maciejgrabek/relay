"""Relay-iTerm gate logic - pure functions over screen text.

No iTerm2 imports here on purpose: this is the load-bearing, fragile part
(reading a permission prompt off a wrapped terminal and deciding what to do),
so it is kept pure and unit-tested against real captured buffers.

Pipeline per active session's screen:
    sanitize(lines) -> reconstruct(lines) -> classify(text) -> Decision

Two gates:
  1. TYPE   - is the screen showing a Claude Code permission prompt
              ("Do you want to proceed?" + a "N. Yes / N. No" menu)?
              A real question (different shape) -> NOTIFY, never inject.
  2. SAFETY - for permission prompts only: extract the command and ask
              danger.sh. SAFE -> INJECT Enter. DANGEROUS -> NOTIFY.
  Any uncertainty -> NOTIFY (fail safe, never inject).
"""
from __future__ import annotations

import os
import hashlib
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

# The one definition of "a horizontal rule line", borrowed rather than
# re-declared so detect_state's anchor and swarm.session_working's anchor
# cannot drift apart. swarm imports only os/re/typing, so there is no cycle.
from swarm import _bracket_line

# The iTerm2 API renders blank cells as NUL (\x00) and uses non-breaking spaces
# (\xa0) inside Claude's UI. Normalize both to plain spaces before matching, or
# "Do you want to proceed?" arrives as "Do\x00you\x00want..." and nothing fires.
_CELL_JUNK = str.maketrans({"\x00": " ", "\xa0": " "})

DANGER_SH = os.path.join(os.path.dirname(__file__), "..", "lib", "danger.sh")

# A permission prompt always contains this exact question.
_PROMPT_MARKER = "Do you want to proceed?"
# Menu option line, e.g. "❯ 1. Yes" or "  2. No" or "1. Yes, and don't ask again".
_OPTION_RE = re.compile(r"^\s*(❯\s*)?(\d+)\.\s+(.*\S)\s*$")
# The selection cursor iTerm renders on the highlighted option.
_CURSOR = "❯"

# The one reason string that means a confirmed dangerous command (as opposed to
# a fail-safe "I could not verify"). Shared with the watcher so the danger sound
# and mascot flinch key off the same value.
DANGEROUS_COMMAND = "dangerous command"


class Action(Enum):
    INJECT = "inject"        # safe permission prompt -> send Enter
    NOTIFY = "notify"        # dangerous / real question / uncertain -> ping human
    NONE = "none"            # nothing actionable on screen


@dataclass
class Decision:
    action: Action
    reason: str
    command: Optional[str] = None     # reconstructed command, when known
    prompt_id: Optional[str] = None   # stable-ish key for debounce
    # is_permission: a "Do you want to proceed?" tool-permission prompt is on
    #   screen (ANY state - cursor anywhere, command parseable or not). NEVER
    #   true for a real multi-choice question. This is what "insane" mode acts on.
    # is_proceed: the stricter case - a permission prompt with the cursor on an
    #   affirmative (Yes) default. This is what "wild" mode acts on.
    # Real questions have BOTH false, so NO mode ever auto-answers them.
    is_permission: bool = False
    is_proceed: bool = False


# The arm ladder, least to most permissive. Ordering is the safety property:
# each rung must approve a SUPERSET of the one below it, and test_gates.py
# enumerates every Decision shape to prove it still does.
ARM_ORDER = ("off", "safe", "wild", "insane", "extreme")


def mode_approves(mode: str, decision: "Decision") -> bool:
    """Would a session armed at `mode` auto-approve this decision?

    THE arm-level policy, in one place. It used to live inline in the watcher's
    prompt handler, which meant the ladder's ordering - the property the whole
    arm model rests on - was four lines buried in an async method with nothing
    asserting it.

      off / shadow - never. shadow observes and records; it must not act.
      safe         - only what the gate itself cleared (Action.INJECT), i.e. a
                     command lib/danger.sh classified non-catastrophic.
      wild         - any permission prompt with the cursor on the affirmative
                     default, WITHOUT classifying the command. Note what that
                     means: a DANGEROUS verdict is approved like any other.
                     Wild is not "safe plus a bit", it is "insane minus one
                     case".
      insane       - any tool-permission prompt at all, including the fail-safe
                     ones (cursor off option 1, unparseable command).
      extreme      - identical to insane here; the idle-push behaviour that
                     distinguishes it lives in the dwell logic, not in this
                     policy.

    A real question has is_permission=False, so NO mode auto-answers one. That
    is the floor under the whole ladder.
    """
    if mode in ("insane", "extreme"):
        return decision.is_permission
    if mode == "wild":
        return decision.is_proceed
    if mode == "safe":
        return decision.action == Action.INJECT
    return False


# Signals that a Claude Code session is actively working (spinner / interrupt
# hint), seen in the live screens. Matched against the sanitized tail.
# NOTE: do NOT include "⏵⏵ accept edits on" - that's a persistent footer shown
# in every session (idle or not); matching it would relabel everything working.
_WORKING_RE = re.compile(
    r"esc to interrupt|tokens\)|·\s*↓\s*[\d.]+k|·\s*↑\s*[\d.]+k|"
    r"\w+(?:ing|izing|ling)…|\(\d+m?\s*\d*s?\s*·",
    re.I)


def detect_state(lines: List[str]) -> str:
    """Best-effort working vs idle from screen content. Only called when there
    is no actionable prompt.

    Scans from the FIRST box-drawing line on screen (swarm._bracket_line - the
    of nothing but "─" that Claude Code draws above its input row) to the
    bottom, for an active-spinner / interrupt signal. No rule -> "idle".
    Deliberately conservative: unknown -> idle, so we never claim 'working'
    for a session that's just sitting at a prompt.

    This used to look at the last 6 non-blank lines, and that fixed window
    was wrong in BOTH directions, because Claude Code renders an unbounded
    number of rows on either side of the box:

    - Completed-tool rows ABOVE the box carry elapsed time and token counts
      ("⎿  Read 244 lines (2m 3s · ↓ 1.2k tokens)"), which _WORKING_RE
      matches. On an idle session one of those sitting near the bottom of
      the window made the screen read "working". detect_state's answer is
      ANDed onto all three typing paths, so that session became permanently
      undeliverable - no message, no timer - and _fire_extreme additionally
      zeroed its dwell anchor on every such tick, so extreme's dwell could
      never accumulate either.
    - Task and agent rows BELOW the footer push the footer out of a 6-line
      window entirely on a busy session, so a genuinely working screen read
      "idle" and relay would type into a live turn.

    Anchoring on the first rule fixes both: the anchor cannot be moved by
    anything appended after it, and the scrollback above it (where the
    completed-tool rows live) is out of scope. It is the same structural
    anchor, and the same first-rule-not-last-rule reasoning, as
    swarm.session_working - see that docstring for why anchoring too EARLY
    (a stale rule up in scrollback, yielding a false "working") is the cheap
    direction and anchoring too LATE is not.

    _WORKING_RE stays the matcher rather than delegating to
    session_working: the two catch different signals (spinner text and
    token/elapsed counters here, the literal "esc to interrupt" footer
    there), and detect_state is called on screens session_working was not
    designed for.
    """
    anchor = None
    for i, l in enumerate(lines):
        if _bracket_line(l.strip()):
            anchor = i
            break
    if anchor is None:
        return "idle"
    for l in lines[anchor:]:
        if _WORKING_RE.search(l):
            return "working"
    return "idle"


# --- the canary --------------------------------------------------------------
# Every "I do not recognise this" path in this module fails safe by returning
# NONE / "idle". That is the right call per screen and the wrong one across a
# fleet: if Claude Code's chrome changes shape, EVERY session reads as a quiet
# idle tab and relay sits there reporting calm while seeing nothing. The panel
# looks identical whether the swarm is genuinely quiet or relay has gone blind.
#
# So: name the anchors this module is load-bearing on, and let the watcher
# notice when a live Claude tab shows none of them. This does not change a
# single decision - it only makes "I cannot read this" observable instead of
# indistinguishable from "nothing is happening".
#
# It has already happened once: the 2026-08-10 round exists because the working
# check was anchored on a bare rule that Claude Code stopped drawing, and the
# only symptom was sessions silently becoming undeliverable.
ANCHORS = ("box", "prompt", "menu", "working")


def chrome_seen(lines: List[str]) -> set:
    """Which of relay's load-bearing screen anchors this frame contains.

    Diagnostic ONLY - nothing in the decision path consults it. Each entry maps
    to a specific thing this module (or swarm.py) matches on:

      box      - _bracket_line, the box chrome around Claude's input row. The
                 structural anchor for detect_state, session_working and draft
                 detection. The one that matters most: a live Claude tab always
                 draws it, so its absence is the strongest "I am blind" signal.
      prompt   - _PROMPT_MARKER, the permission question.
      menu     - _OPTION_RE, the numbered option list.
      working  - _WORKING_RE, the spinner / interrupt footer.

    `prompt`, `menu` and `working` are legitimately absent from a calm idle
    screen, which is why blind() below keys on `box` alone.
    """
    seen = set()
    for l in lines:
        s = l.strip()
        if not s:
            continue
        if "box" not in seen and _bracket_line(s):
            seen.add("box")
        if "prompt" not in seen and s.startswith(_PROMPT_MARKER):
            seen.add("prompt")
        if "menu" not in seen and _OPTION_RE.match(l):
            seen.add("menu")
        if "working" not in seen and _WORKING_RE.search(l):
            seen.add("working")
        if len(seen) == len(ANCHORS):
            break
    return seen


def blind(raw_lines: List[str], hard_eols: Optional[List[bool]] = None) -> bool:
    """True when a screen carries NONE of the chrome relay reads.

    Keyed on the box rule rather than on all four anchors: a calm idle session
    genuinely has no prompt, no menu and no spinner, so requiring those would
    fire constantly. A live Claude Code tab always draws its input box.

    A blank screen is NOT blind - a tab that has scrolled to empty, or is
    starting up, has nothing to recognise and nothing to be wrong about.
    Reporting it would make the canary noisy on exactly the frames that carry
    no information, and a canary that cries during startup is one the operator
    silences.

    The CALLER is responsible for only asking about tabs where Claude is the
    foreground job, and for requiring this to persist - see the watcher's
    blind_ticks. One frame proves nothing: the box is legitimately absent mid-
    redraw, during a full-screen takeover, and for the first second of startup.
    """
    lines = reconstruct_lines(raw_lines, hard_eols)
    if not any(l.strip() for l in lines):
        return False
    return not chrome_seen(lines)


def sanitize(line: str) -> str:
    """Map iTerm cell-junk (NUL, nbsp) to spaces and strip trailing blanks."""
    return line.translate(_CELL_JUNK).rstrip()


def reconstruct_lines(raw_lines: List[str], hard_eols: Optional[List[bool]] = None) -> List[str]:
    """Join soft-wrapped rows back into logical lines.

    iTerm marks a row with hard_eol=False when the text wrapped to the next row
    rather than ending. Re-joining those gives the original (unwrapped) command
    line exactly. If hard_eols is None (e.g. in tests with pre-joined text),
    lines are returned sanitized but unjoined.
    """
    if hard_eols is None:
        return [sanitize(l) for l in raw_lines]
    out: List[str] = []
    buf = ""
    for text, hard in zip(raw_lines, hard_eols):
        # Map cell-junk but DON'T rstrip a soft-wrapped row: its trailing chars
        # are part of the wrapped command. Only rstrip once the logical line ends.
        buf += text.translate(_CELL_JUNK)
        if hard:
            out.append(buf.rstrip())
            buf = ""
    if buf:
        out.append(buf.rstrip())
    return out


def _is_permission_prompt(lines: List[str]) -> bool:
    """A real permission prompt renders the marker as ITS OWN line. A command
    that merely echoes the text (e.g. echo "Do you want to proceed?") has the
    marker embedded mid-line, deeper-indented, and must NOT count - otherwise a
    crafted command could fake the type gate. Require a line whose stripped text
    STARTS with the marker (Claude renders nothing before it on that line)."""
    return any(l.strip().startswith(_PROMPT_MARKER) for l in lines)


def _prompt_marker_index(lines: List[str]) -> Optional[int]:
    for i, l in enumerate(lines):
        if l.strip().startswith(_PROMPT_MARKER):
            return i
    return None


def _looks_like_real_question(lines: List[str]) -> bool:
    """Heuristic: Claude's AskUserQuestion / multi-select prompts do NOT contain
    the exact "Do you want to proceed?" tool-permission marker. We treat anything
    that is a menu WITHOUT that marker as a real question -> hands off.
    """
    has_options = sum(1 for l in lines if _OPTION_RE.match(l)) >= 2
    return has_options and not _is_permission_prompt(lines)


def _cursor_on_first_option(lines: List[str]) -> bool:
    """The default (option 1) must be the highlighted one before we accept it.

    Only real MENU OPTION lines count - a line matching '[❯] N. text'. We must
    NOT treat a bare ❯ on any other line as the cursor: Claude renders the
    user's own chat messages as '❯ <text>', and the shell prompt is a bare '❯';
    an earlier match on one of those used to make every screen look like a menu
    with the cursor on the wrong row (the false 'cursor not on option 1' spam).

    Walk only option lines; the first one carrying the cursor must be option 1.
    """
    for l in lines:
        m = _OPTION_RE.match(l)
        if not m:
            continue                      # not a menu option - ignore chat/prompt ❯
        if m.group(1):                    # this option line carries the ❯ cursor
            if m.group(2) != "1":
                return False              # cursor on option 2/3... -> fail safe
            # Number is 1, but also require the option TEXT to be affirmative.
            # We send Enter to accept the highlighted default; never accept a
            # highlighted "No"/"Cancel"/etc. just because it's numbered 1.
            text = m.group(3).strip().lower()
            if re.match(r"(no|cancel|don'?t|stop|abort|quit|skip)\b", text):
                return False
            return text.startswith("yes") or "proceed" in text or "allow" in text
    return False


def _prompt_id(lines: List[str], cmd: Optional[str]) -> Optional[str]:
    """Stable identity for a prompt instance, used to debounce the 2s poll loop.

    Must be the SAME across re-reads of the same prompt and DIFFERENT for a new
    one. Prefer the full command (hashed - not a lossy 60-char prefix, which
    collided for commands sharing a prefix). With no parseable command, hash the
    menu option lines plus marker presence. None only when there's nothing
    prompt-like at all.
    """
    if cmd:
        return "c:" + hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:16]
    # No parseable command: build the id from the NORMALIZED option text so a
    # redrawing menu (moving ❯ cursor, trailing-space/ellipsis churn) yields the
    # SAME id across polls. We key on "<n>. <core label>" with the cursor and
    # trailing punctuation/whitespace stripped - the part that identifies the
    # prompt, not its render state.
    norm = []
    for l in lines:
        m = _OPTION_RE.match(l)
        if m:
            core = re.sub(r"[\s.]+$", "", m.group(3).strip().lower())
            norm.append(f"{m.group(2)}.{core}")
    basis = "|".join(norm)
    if _is_permission_prompt(lines):
        basis += "|PROMPT"
    if not basis:
        return None
    return "p:" + hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]


def extract_command(lines: List[str]) -> Optional[str]:
    """Pull the command block Claude shows above the permission prompt.

    Claude's box is INDENTATION-structured (verified against live screens):

        ' Bash command'                              <- header, indent 1
        '   set -a; source ...; poetry run pytest'   <- command, indent 3
        '   tests/... -q 2>&1 | tail -20'            <- wrapped, indent 3
        '   Run the three new test files (expect ..)'<- human summary, indent 3
        " 'set -a' changes shell option state ..."   <- DETECTOR REASON, indent 1
        ' Do you want to proceed?'                    <- prompt marker, indent 1

    So the command lines are exactly the ones MORE indented than the header.
    We collect contiguous deeper-indented lines after the header and stop the
    moment indentation drops back to header level (the detector-reason text).
    Then drop a trailing prose summary line. This prevents the detector's own
    explanation from being swept into the command and mis-classified.
    Returns None if we cannot cleanly locate it -> caller fails safe.
    """
    start = header_indent = None
    for i, l in enumerate(lines):
        if re.search(r"\bBash command\b", l, re.I):
            start = i + 1
            header_indent = len(l) - len(l.lstrip())
            break
    # End at the REAL prompt marker line (stripped-startswith), never at a mere
    # substring inside a command - that boundary bug could truncate a dangerous
    # tail and auto-approve the safe-looking head.
    end = _prompt_marker_index(lines)
    if start is None or end is None or start >= end:
        return None

    block = []
    for l in lines[start:end]:
        if not l.strip():
            continue
        indent = len(l) - len(l.lstrip())
        if indent <= header_indent:
            break  # dropped back to header level: detector reason / chrome
        block.append(l.strip())
    if not block:
        return None
    # Drop a trailing human summary (prose: no shell metacharacters / verbs).
    if len(block) > 1 and not re.search(
            r"[;&|/$\"'=]|--|\b(git|grep|cd|rm|ls|cat|echo|run|poetry|npm|make)\b",
            block[-1]):
        block = block[:-1]
    cmd = " ".join(block).strip()
    return cmd or None


def is_dangerous(command: str) -> Optional[bool]:
    """Run danger.sh. True=dangerous, False=safe, None=could not classify."""
    try:
        r = subprocess.run(
            ["bash", "-c",
             f'source "{DANGER_SH}"; relay_is_dangerous "$1"', "_", command],
            capture_output=True, timeout=5,
        )
        # relay_is_dangerous: exit 0 = dangerous, 1 = safe
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        return None
    except Exception:
        return None


def _is_actively_working(lines: List[str]) -> bool:
    """A live spinner / interrupt hint in the LAST few lines means the session
    is mid-task right now. Any 'Do you want to proceed?' text above it is then a
    STALE/already-answered prompt still in scrollback, not a live one - we must
    not treat it as actionable (that's the 'spinning but shows blocked' bug)."""
    tail = [l for l in lines if l.strip()][-4:]
    return any(re.search(r"esc to interrupt|·\s*↓\s*[\d.]+k|·\s*↑\s*[\d.]+k|"
                         r"\w+(?:ing|izing|ling)…", l, re.I) for l in tail)


def classify(raw_lines: List[str], hard_eols: Optional[List[bool]] = None) -> Decision:
    """Top-level gate. Feed it a screen's raw lines (+ optional hard_eol flags)."""
    lines = reconstruct_lines(raw_lines, hard_eols)

    # If the session is actively working, any prompt text on screen is stale
    # (scrolled-up, already answered). Don't act on it - it's working.
    if _is_actively_working(lines):
        return Decision(Action.NONE, "actively working")

    if not _is_permission_prompt(lines):
        if _looks_like_real_question(lines):
            return Decision(Action.NOTIFY, "real question - hands off",
                            prompt_id=_prompt_id(lines, None))
        return Decision(Action.NONE, "no actionable prompt")

    # The marker is present, but the menu may still be RENDERING: Claude prints
    # "Do you want to proceed?" a beat before it draws the "N. Yes / N. No" lines
    # and moves the ❯ cursor onto one. Acting on that half-drawn frame caused a
    # transient "cursor not on option 1" escalation (a flash of blocked during
    # quick prompts). A complete menu = >=2 option lines AND a cursor on one of
    # them. Until then, treat as not-yet-actionable and wait for the next poll.
    n_opts = sum(1 for l in lines if _OPTION_RE.match(l))
    has_cursor = any(_OPTION_RE.match(l) and _OPTION_RE.match(l).group(1) for l in lines)
    if n_opts < 2 or not has_cursor:
        return Decision(Action.NONE, "prompt still rendering")

    # It IS a tool-permission prompt (not a question) -> is_permission=True for
    # every path below. "insane" mode acts on this alone.
    if not _cursor_on_first_option(lines):
        return Decision(Action.NOTIFY, "cursor not on option 1 - fail safe",
                        prompt_id=_prompt_id(lines, None), is_permission=True)

    # Cursor on an affirmative default -> is_proceed=True too. "wild" mode acts
    # on this regardless of command classification.
    cmd = extract_command(lines)
    if cmd is None:
        # Distinguish the common case: a large command (e.g. a heredoc) whose
        # 'Bash command' header has scrolled off the top of the visible screen,
        # so we genuinely can't see the whole command. Honest reason in the log.
        reason = ("command too large to verify (header off-screen) - fail safe"
                  if not any(re.search(r"\bBash command\b", l, re.I) for l in lines)
                  else "could not parse command - fail safe")
        return Decision(Action.NOTIFY, reason, prompt_id=_prompt_id(lines, None),
                        is_permission=True, is_proceed=True)

    danger = is_dangerous(cmd)
    pid = _prompt_id(lines, cmd)
    if danger is True:
        return Decision(Action.NOTIFY, DANGEROUS_COMMAND, command=cmd,
                        prompt_id=pid, is_permission=True, is_proceed=True)
    if danger is False:
        return Decision(Action.INJECT, "safe permission prompt", command=cmd,
                        prompt_id=pid, is_permission=True, is_proceed=True)
    return Decision(Action.NOTIFY, "danger.sh could not classify - fail safe",
                    command=cmd, prompt_id=pid, is_permission=True, is_proceed=True)
