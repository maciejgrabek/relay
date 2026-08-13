"""relay selftest - check relay against the LIVE terminal, not a stub.

Every automated suite in this repo drives a stub watcher. That is the right
trade for logic, and it is why five separate "MANUAL GATE, unrun" notes have
accumulated in docs/IDEAS.md since 2026-08-09: the only way to check relay
against a real iTerm2 window was a forty-minute click-through nobody was ever
going to do twice.

This is the part of that gate a machine can do. It connects to iTerm2, reads
every tab exactly the way the watcher does, and answers the one question the
unit tests structurally cannot:

    can relay still READ the sessions in front of it?

It does NOT spawn anything, type anything, or change any state. Read-only, on
purpose - a selftest that mutates the swarm is one you hesitate to run, and a
check you hesitate to run is the check you skip.

The other half is the payoff: any tab relay cannot read can be captured
straight into iterm/fixtures/screens/ with --capture. That closes the loop the
canary opens - the panel says "CANNOT READ api-worker", one command turns that
frame into a permanent regression fixture, and test_state.py fails on it until
the classifier can read it again. Before this, growing the corpus meant
hand-copying a terminal buffer, which is why it only ever happened reactively,
once, after a break.
"""
from __future__ import annotations

import os
import re
import time
from typing import List, Optional

import gates
import swarm

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "screens")

# ANSI, kept local rather than imported from app.py - this runs as a plain CLI
# verb with no Textual app and no theme loaded.
_G, _R, _Y, _D, _0 = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# Foreground job names a Claude Code session runs under. Claude ships as a
# Node program, so `node` is what iTerm2 reports; the rest are here for other
# packagings and cost nothing.
#
# POSITIVE match, deliberately - and this is the one place relay's usual
# fail-safe direction is inverted. The watcher runs its classifier on ANY
# non-shell job, because failing to classify a real Claude tab is the
# expensive mistake there. Here the expensive mistake is the opposite: calling
# a non-Claude tab "unreadable" raises a false alarm and, with --capture,
# writes a junk frame into the regression corpus that then fails the suite
# forever. Relay's own panel is exactly such a tab - it draws box-rule chrome
# of its own and reports `python3` (or `terminal-notifier` mid-alert).
#
# Anything not matched is REPORTED as skipped rather than silently dropped, so
# a future packaging change shows up as "not recognised" instead of as a tab
# quietly falling out of coverage.
CLAUDE_JOBS = ("node", "claude", "bun", "deno")


def looks_like_claude(job: str) -> bool:
    j = (job or "").strip().lower()
    return any(j == c or j.startswith(c) for c in CLAUDE_JOBS)


def fixture_name(title: str, state: str, now: Optional[float] = None) -> str:
    """A stable, filesystem-safe fixture name.

    Carries the classified state, because a corpus of `capture_1.txt` tells a
    future reader nothing about what each frame was supposed to prove - and the
    existing corpus is named for what it shows (idle_accept_edits,
    working_with_agent_rows), which is the convention worth keeping.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "session").lower()).strip("_")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"unread_{slug or 'session'}_{state}_{stamp}"


def fixture_text(title: str, state: str, job: str, lines: List[str]) -> str:
    """A capture in the same shape as the hand-made fixtures: a '#' header
    naming what this is and when it came from, then the screen verbatim.

    The header matters more than it looks. The existing corpus carries
    "Refresh when the UI changes" precisely because a frame with no provenance
    is one nobody dares delete, and a corpus nobody dares prune stops being
    curated.
    """
    head = [
        f"# Captured by `relay selftest --capture` on "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}, through relay's own read path.",
        f"# tab: {title}   job: {job}   classified: {state}",
        "# RELAY COULD NOT READ THIS FRAME - none of the chrome anchors "
        "(box rule,",
        "# permission marker, option menu, working footer) matched. If that is "
        "wrong,",
        "# the classifier needs updating; test_state.py fails on this file "
        "until it can",
        "# read it. If the tab genuinely was not running Claude, delete this "
        "file.",
    ]
    return "\n".join(head + list(lines)) + "\n"


def write_fixture(name: str, text: str, root: Optional[str] = None) -> str:
    root = root or FIXTURE_DIR
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name + ".txt")
    with open(path, "w") as fh:
        fh.write(text)
    return path


def report_line(title: str, job: str, readable: bool, state: str,
                decision: str, anchors: set) -> str:
    """One tab, one line. Plain text so it is greppable and pasteable into an
    issue - this is a diagnostic, not a dashboard."""
    if not looks_like_claude(job):
        why = "shell" if swarm.is_shell_job(job) else "not recognised as Claude"
        return (f"  {_D}·{_0} {title:<24} "
                f"{_D}skipped - {why} (job={job or '?'}){_0}")
    if not readable:
        return (f"  {_R}✗{_0} {title:<24} {_R}CANNOT READ{_0} "
                f"{_D}job={job or '?'}{_0}")
    seen = ",".join(sorted(anchors)) or "-"
    return (f"  {_G}✓{_0} {title:<24} {state:<9} {_D}{seen}{_0}  "
            f"{_D}{decision}{_0}")


def summarise(n_tabs: int, n_claude: int, n_unread: int,
              captured: List[str], n_skipped: int = 0) -> List[str]:
    """The verdict, and what to do about it.

    A selftest that prints only a pass/fail is one the operator has to
    interpret; the whole reason the manual gates went unrun is that knowing
    WHAT to do next was itself work.
    """
    out = []
    if n_skipped:
        # Named, not silently dropped: if Claude's packaging changes, this line
        # is the difference between "coverage narrowed" and a tab quietly
        # falling out of the check with everything still green.
        out.append(f"  {_D}·{_0} {n_skipped} tab(s) skipped (shell, or job "
                   f"not in {'/'.join(CLAUDE_JOBS)}) - see above")
    if not n_claude:
        out.append(f"  {_Y}!{_0} no tab is running Claude - nothing to verify."
                   f" Relay's read path is untested by this run.")
        return out
    if not n_unread:
        out.append(f"  {_G}✓{_0} relay can read all {n_claude} Claude "
                   f"tab(s) of {n_tabs}.")
        return out
    out.append(f"  {_R}✗{_0} relay cannot read {n_unread} of {n_claude} "
               f"Claude tab(s).")
    if captured:
        for p in captured:
            out.append(f"      captured -> {p}")
        out.append(f"      {_D}now: python3 iterm/test_state.py  "
                   f"(it will FAIL until the classifier reads these){_0}")
        out.append(f"      {_D}delete any capture whose tab was not actually "
                   f"running Claude.{_0}")
    else:
        out.append(f"      {_D}re-run with --capture to save these frames as "
                   f"regression fixtures.{_0}")
    return out


async def run(connection, capture: bool = False,
              root: Optional[str] = None) -> int:
    """Read every tab through the watcher's own path and report. Returns an
    exit code: 0 when every Claude tab is readable, 1 when any is not."""
    import iterm2
    from watcher import _extract_lines

    app = await iterm2.async_get_app(connection)
    rows, captured = [], []
    n_tabs = n_claude = n_unread = n_skipped = 0
    own = os.environ.get("ITERM_SESSION_ID", "").split(":", 1)[-1]

    for win in app.terminal_windows:
        for tab in win.tabs:
            for s in tab.sessions:
                n_tabs += 1
                # Relay's own tab is chrome, not a subject - and reading the
                # panel through the panel is the RELAY-inside-RELAY case the
                # preview pane already refuses.
                if s.session_id == own:
                    continue
                try:
                    job = await s.async_get_variable("jobName") or ""
                except Exception:
                    job = ""
                try:
                    title = await s.async_get_variable("autoName") or s.session_id[:8]
                except Exception:
                    title = s.session_id[:8]
                if not looks_like_claude(job):
                    rows.append(report_line(title, job, True, "", "", set()))
                    n_skipped += 1
                    continue
                n_claude += 1
                try:
                    contents = await s.async_get_screen_contents()
                    raw, hard = _extract_lines(contents)
                except Exception as e:
                    rows.append(f"  {_R}✗{_0} {title:<24} screen unreadable: {e}")
                    n_unread += 1
                    continue
                lines = gates.reconstruct_lines(raw, hard)
                anchors = gates.chrome_seen(lines)
                readable = not gates.blind(raw, hard)
                state = gates.detect_state(lines)
                # The decision is reported but never acted on: this verb is
                # read-only, so the operator sees what relay WOULD do without
                # relay doing it.
                try:
                    decision = gates.classify(raw, hard).reason
                except Exception as e:
                    decision = f"classify raised: {e}"
                rows.append(report_line(title, job, readable, state,
                                        decision, anchors))
                if not readable:
                    n_unread += 1
                    if capture:
                        body = [l for l in lines if l.strip()]
                        captured.append(write_fixture(
                            fixture_name(title, state),
                            fixture_text(title, state, job, body), root))

    print("relay selftest - live read check (read-only, nothing was typed)")
    print()
    for r in rows:
        print(r)
    print()
    for line in summarise(n_tabs, n_claude, n_unread, captured,
                          n_skipped):
        print(line)
    return 1 if n_unread else 0
