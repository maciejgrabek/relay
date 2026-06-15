"""Tests for the gate logic, built from REAL prompts the user hit on 2026-06-11.

Run: python3 iterm/test_gates.py             (no deps - has a __main__ runner)
 or: ./test/run.sh                            (the whole suite, bash + Python)
 or: python3 -m pytest iterm/test_gates.py    (only if pytest is installed)

The screens mimic what the iTerm2 API actually returns: blank cells as NUL
(\x00) and Claude's UI nbsp (\xa0), so we exercise sanitize() for real.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from gates import (  # noqa: E402
    classify, Action, sanitize, extract_command, reconstruct_lines,
    _cursor_on_first_option,
)

# Derived at runtime so the fixtures carry this machine's real home path
# instead of a hardcoded one (the classifier only sees the text, but this keeps
# the corpus honest and portable across machines).
HOME = os.path.expanduser("~")


def _screen(*lines):
    """Build a raw screen with NUL-padded blanks like the real API returns."""
    return list(lines)


# --- Fixture 1: cd;echo;ls;grep gather, "cd with output redirection" reason ---
FIX1 = _screen(
    " Bash command",
    "",
    f'   cd {HOME}/work/myproject/.wt-feature; echo "=== plans dir ==="; ls docs/plans/',
    '   2>/dev/null | tail -6; grep -nA12 "class ResolutionSnapshot" src/shared/models/overlay.py',
    "   Gather plans-dir, parity test, snapshot shape",
    "",
    " Compound command contains cd with output redirection - manual approval required",
    "",
    "\x00Do\x00you\x00want\x00to\x00proceed?",
    "❯\x001.\x00Yes",
    "\x00\x002.\x00No",
)

# --- Fixture 2: em-dash scan + commit, "Contains ansi_c_string" reason ---
FIX2 = _screen(
    " Bash command",
    "",
    f"   cd {HOME}/work/myproject/.wt-feature; grep -c $'\u2014' plan.md;",
    '   grep -niE "\\bTBD\\b" plan.md | head; git add plan.md && git commit -q -m "spec" && git log --oneline -1',
    "   Scan plan for em-dashes and commit",
    "",
    " Contains ansi_c_string",
    "",
    "Do you want to proceed?",
    "❯ 1. Yes",
    "  2. No",
)

# --- Fixture 3: the classifier meta-command, "shell syntax (string)" reason ---
FIX3 = _screen(
    " Bash command",
    "",
    f"   cd {HOME}/work/relay; source lib/danger.sh; CMD2='...embedded...';",
    '   if relay_is_dangerous "$CMD2"; then echo DANGEROUS; else echo SAFE; fi',
    "   Classify the meta-command",
    "",
    " Contains shell syntax (string) that cannot be statically analyzed",
    "",
    "Do you want to proceed?",
    "❯ 1. Yes",
    "  2. No",
)

# --- Fixture 4: REAL payroll prompt (2026-06-11 live capture) that exposed the
# detector-reason-contamination bug. The "'set -a' changes shell option state"
# line at header indent must be EXCLUDED from the extracted command. ---
FIX_PAYROLL = _screen(
    "────────────────────────────────────────────────────────────",
    " Bash command",
    "",
    "   set -a; source config/test.env; set +a; PYTHONPATH=src poetry run pytest",
    "   tests/integration_engine/test_field_catalog_repository.py tests/integration_engine/x.py",
    '   tests/integration_engine/test_monthly_delta_field_repository.py -o addopts="" -q 2>&1 | tail -20',
    "   Run the three new test files (expect 6 passed)",
    "",
    " 'set -a' changes shell option state (allexport/keyword/) defeats static env-var analysis",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. No",
    " Esc to cancel · Tab to amend · ctrl+e to explain",
)

# --- Counter: a DANGEROUS command spliced into a safe-looking chain ---
FIX_DANGER = _screen(
    " Bash command",
    "",
    '   cat notes.md; git push --force origin main',
    "   Push the branch",
    "",
    "Do you want to proceed?",
    "❯ 1. Yes",
    "  2. No",
)

# --- Counter: a REAL question (AskUserQuestion shape, NO proceed marker) ---
FIX_QUESTION = _screen(
    "  Which approach should we take?",
    "",
    "❯ 1. Rewrite the parser",
    "  2. Patch the existing one",
    "  3. Leave it",
)

# --- Counter: cursor NOT on option 1 (user pre-moved to No) ---
FIX_CURSOR2 = _screen(
    " Bash command",
    "",
    "   rm -rf build/",
    "   Clean build dir",
    "",
    "Do you want to proceed?",
    "  1. Yes",
    "❯ 2. No",
)

# --- Counter: nothing actionable (a working session) ---
FIX_IDLE = _screen(
    "  Improving the gate logic...",
    "  ⏵⏵ accept edits on",
    "❯",
)

# --- Regression: Claude renders the USER's chat messages as '❯ <text>', and the
# shell prompt is a bare '❯'. None of these are a menu. This screen (taken from
# the real spam incident) must classify NONE, not NOTIFY - the ❯ chat lines must
# NOT be mistaken for a menu cursor on a non-first option. ---
FIX_CHAT_NOISE = _screen(
    "⏺ Done",
    "❯ wow - this N feature is super cool",
    "❯ you are sending too many alerts - this is one",
    "❯ and you were blocked - it should be approved",
    "· Manifesting… (14m · ↓ 58.9k tokens)",
    "────────────────────────────",
    "❯",
    "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt",
)


# --- Regression: a session actively WORKING with an already-answered prompt
# still visible in scrollback must classify NONE ('actively working'), not
# NOTIFY - otherwise the TUI shows it stuck 'blocked' while it's spinning. ---
FIX_WORKING_STALE_PROMPT = _screen(
    "Do you want to proceed?",      # stale - already answered, scrolled up
    "❯ 1. Yes",
    "  2. No",
    "⏺ Yes - proceeding. Working on the task now...",
    "· Manifesting… (2m 13s · ↓ 5.1k tokens)",
    "❯",
    "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt",
)

# --- CRITICAL regression: a command that ECHOES the prompt marker must NOT let
# a dangerous tail be truncated off and the safe head auto-approved. The real
# prompt marker is its own line (stripped-startswith); an embedded one is not. ---
FIX_MARKER_IN_CMD = _screen(
    " Bash command",
    "",
    '   echo "Do you want to proceed?"; rm -rf /important',
    "   sneaky",
    "",
    "Do you want to proceed?",
    "❯ 1. Yes",
    "  2. No",
)

# --- CRITICAL regression: option 1 highlighted but its text is "No" - must NOT
# auto-accept just because the cursor is on number 1. ---
FIX_NO_IS_DEFAULT = _screen(
    " Bash command",
    "",
    "   grep foo src/",
    "   search",
    "",
    "Do you want to proceed?",
    "❯ 1. No",
    "  2. Yes",
)


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    return cond


def run():
    ok = True

    # sanitize strips NUL/nbsp
    ok &= check("sanitize NUL->space",
                sanitize("Do\x00you\x00want\x00to\x00proceed?") == "Do you want to proceed?")

    # The three real fixtures must all -> INJECT (safe permission prompt)
    for nm, fx in (("fix1", FIX1), ("fix2", FIX2), ("fix3", FIX3)):
        d = classify(fx)
        ok &= check(f"{nm} -> INJECT ({d.reason})", d.action == Action.INJECT)
        ok &= check(f"{nm} extracted a command", bool(d.command))

    # Real payroll prompt: extracts the command WITHOUT the detector-reason text.
    cmd = extract_command(FIX_PAYROLL)
    ok &= check("payroll extracts a command", bool(cmd))
    ok &= check("payroll command starts with 'set -a'",
                bool(cmd) and cmd.startswith("set -a"))
    ok &= check("payroll EXCLUDES detector-reason text",
                bool(cmd) and "changes shell option" not in cmd)
    ok &= check("payroll EXCLUDES prompt chrome",
                bool(cmd) and "Esc to cancel" not in cmd and "proceed" not in cmd)
    dp = classify(FIX_PAYROLL)
    ok &= check(f"payroll -> INJECT ({dp.reason})", dp.action == Action.INJECT)

    # Dangerous splice -> NOTIFY for the RIGHT reason (caught dangerous, not a
    # parse failure). The command must parse AND be flagged dangerous.
    d = classify(FIX_DANGER)
    ok &= check(f"danger -> NOTIFY ({d.reason})", d.action == Action.NOTIFY)
    ok &= check("danger caught as dangerous (not parse-fail)",
                d.reason == "dangerous command" and bool(d.command))

    # Real question -> NOTIFY (hands off)
    d = classify(FIX_QUESTION)
    ok &= check(f"question -> NOTIFY ({d.reason})", d.action == Action.NOTIFY)

    # Cursor on option 2 -> NOTIFY (fail safe), even though command would parse
    d = classify(FIX_CURSOR2)
    ok &= check(f"cursor-on-2 -> NOTIFY ({d.reason})", d.action == Action.NOTIFY)

    # Idle screen -> NONE
    d = classify(FIX_IDLE)
    ok &= check(f"idle -> NONE ({d.reason})", d.action == Action.NONE)

    # Chat-noise screen (lots of '❯ ...' user messages) -> NONE, never NOTIFY.
    d = classify(FIX_CHAT_NOISE)
    ok &= check(f"chat-noise -> NONE ({d.reason})", d.action == Action.NONE)

    # Actively working with a stale answered prompt in scrollback -> NONE, not
    # NOTIFY (the 'spinning but shows blocked' bug).
    d = classify(FIX_WORKING_STALE_PROMPT)
    ok &= check(f"working+stale-prompt -> NONE ({d.reason})", d.action == Action.NONE)

    # CRITICAL: command echoing the marker must extract the FULL command (incl.
    # the dangerous tail) and therefore NOT auto-approve -> classified dangerous.
    cmd = extract_command(FIX_MARKER_IN_CMD)
    ok &= check("marker-in-cmd: full command extracted (tail kept)",
                bool(cmd) and "rm -rf /important" in cmd)
    d = classify(FIX_MARKER_IN_CMD)
    ok &= check(f"marker-in-cmd -> NOT inject ({d.action.value})",
                d.action != Action.INJECT)

    # CRITICAL: option 1 == 'No' highlighted -> must NOT inject.
    d = classify(FIX_NO_IS_DEFAULT)
    ok &= check(f"option-1-is-No -> NOT inject ({d.action.value}/{d.reason})",
                d.action != Action.INJECT)

    # _cursor_on_first_option direct checks
    ok &= check("cursor detects option1", _cursor_on_first_option(["❯ 1. Yes", "  2. No"]))
    ok &= check("cursor rejects option2", not _cursor_on_first_option(["  1. Yes", "❯ 2. No"]))
    # The bug: a bare '❯ chat message' must NOT be read as the menu cursor.
    ok &= check("bare ❯ chat line is not a cursor",
                not _cursor_on_first_option(["❯ you are sending too many alerts", "  some text"]))
    # prompt_id is stable across re-reads (same screen -> same id) and present
    from gates import _prompt_id
    a = _prompt_id(["❯ 1. Yes", "  2. No", "Do you want to proceed?"], "grep foo")
    b = _prompt_id(["❯ 1. Yes", "  2. No", "Do you want to proceed?"], "grep foo")
    ok &= check("prompt_id stable across re-reads", a == b and a is not None)
    ok &= check("prompt_id None when nothing prompt-like", _prompt_id(["just idle", "❯"], None) is None)

    # reconstruct_lines re-joins soft wraps
    joined = reconstruct_lines(["foo bar ", "baz qux"], hard_eols=[False, True])
    ok &= check("soft-wrap rejoin", joined == ["foo bar baz qux"])

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
