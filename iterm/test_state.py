"""Tests for detect_state - working vs idle from real Claude Code screen tails.

Fixtures are taken from actual iTerm2 API captures. The key trap: the
'⏵⏵ accept edits on (shift+tab to cycle)' footer is shown in EVERY session,
idle or not - only the '· esc to interrupt' suffix appears while working.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from gates import detect_state  # noqa: E402

CASES = [
    # working: live spinner with elapsed time + token counter
    (["✢ Improvising… (2m 54s · ↓ 10.1k tokens)", "  ⎿ Build gate logic"], "working"),
    (["· Marinating… (1m 2s · ↓ 2.8k tokens)"], "working"),
    # working: footer WITH the interrupt hint (full string, as rendered live)
    (["~/Work/relay", "❯",
      "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt · ctrl+t to hide tasks"], "working"),
    # idle: bare prompt
    (["Resume this session with:", "claude --resume abc", "~/Work took 1d8h", "❯"], "idle"),
    (["s&code_challenge=XQ", "Successfully logged in", "✅ Done", "❯"], "idle"),
    # idle: a finished shell command sitting at the prompt
    (["set -a; source config/test.env; set +a", "❯ relay"], "idle"),
    # idle: the accept-edits footer ALONE (no interrupt hint) must NOT be working
    (["some output", "❯", "  ⏵⏵ accept edits on (shift+tab to cycle)"], "idle"),
]


def run():
    ok = True
    for lines, exp in CASES:
        got = detect_state(lines)
        flag = "PASS" if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(flag, f"exp {exp:8} got {got:8} | {lines[-1][:55]!r}")
    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
