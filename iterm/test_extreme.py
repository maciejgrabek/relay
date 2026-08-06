"""Extreme insane mode suite: the draft-guard helper, config knobs, watcher
arming/firing/exhaustion, TUI + statusbar chrome.

Run: python3 iterm/test_extreme.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"

ok = True


def chk(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond


# Screen tails. READY is a Claude idle screen with an EMPTY input box;
# DRAFT has operator text typed after the '>' but not submitted.
READY = ["╭──────────────╮", "│ >            │", "╰──────────────╯",
         "  ? for shortcuts"]
DRAFT = ["╭──────────────╮", "│ > fix the login bug   │",
         "╰──────────────╯", "  ? for shortcuts"]
SHELL = ["some output", "~/Work $"]


def test_prompt_line_empty():
    import swarm
    chk("empty input box -> True", swarm.prompt_line_empty(READY))
    chk("typed draft -> False", not swarm.prompt_line_empty(DRAFT))
    chk("shell prompt (no box) -> False", not swarm.prompt_line_empty(SHELL))
    chk("empty screen -> False", not swarm.prompt_line_empty([]))
    chk("READY still passes claude_prompt_ready",
        swarm.claude_prompt_ready(READY))


if __name__ == "__main__":
    test_prompt_line_empty()
    print("ALL PASSED" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
