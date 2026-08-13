"""Tests for the pure parts of `relay selftest` - naming, fixture shape, and
the report/summary text.

Run: python3 iterm/test_selftest.py

The iTerm2-driving half (selftest.run) is deliberately untested here: it needs
a live terminal, and pretending otherwise with a mock would be exactly the
stub-shaped confidence this verb exists to escape.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selftest  # noqa: E402

_fails = []


def check(label, cond):
    print(("  OK   " if cond else " FAIL  ") + label)
    if not cond:
        _fails.append(label)
    return cond


def main():
    # --- fixture naming: the corpus is named for what each frame SHOWS
    # (idle_accept_edits, working_with_agent_rows). A capture_1.txt tells a
    # future reader nothing about what the frame was supposed to prove.
    n = selftest.fixture_name("api-worker", "idle", now=0)
    check("a capture is named for the tab and the state it classified as",
          "api_worker" in n and "idle" in n)
    check("captures are marked unread so they are never mistaken for a "
          "curated pass fixture", n.startswith("unread_"))
    check("a capture name is timestamped, so two captures of one tab do not "
          "overwrite each other",
          selftest.fixture_name("api", "idle", now=0)
          != selftest.fixture_name("api", "idle", now=90000))
    check("a hostile tab title cannot escape the fixture directory",
          "/" not in selftest.fixture_name("../../etc/passwd", "idle")
          and ".." not in selftest.fixture_name("../../etc/passwd", "idle"))
    check("an empty title still yields a usable name",
          selftest.fixture_name("", "idle").startswith("unread_session"))
    check("a title of only punctuation still yields a usable name",
          "session" in selftest.fixture_name("!!!", "idle"))

    # --- fixture text: provenance is what makes a corpus prunable. The
    # existing fixtures carry "Refresh when the UI changes" for this reason.
    body = ["some output", "❯ "]
    txt = selftest.fixture_text("api-worker", "idle", "claude", body)
    check("the capture keeps the screen verbatim",
          all(l in txt for l in body))
    check("the capture records which tab and job it came from",
          "api-worker" in txt and "claude" in txt)
    check("the capture says WHY it was written, not just that it was",
          "COULD NOT READ" in txt)
    check("the capture names the anchors that failed, so a reader knows what "
          "to look for", "box rule" in txt and "option menu" in txt)
    check("the capture says how to act on it", "test_state.py" in txt)
    check("the capture says when to DELETE it - a corpus nobody dares prune "
          "stops being curated", "delete this file" in txt.lower())
    check("every header line is a comment, so the loader's '#' strip leaves "
          "only screen content",
          all(l.startswith("#") for l in txt.splitlines()
              if l not in body and l.strip()))

    # A written capture must round-trip through the SAME loader the corpus
    # uses, or it is a file that only looks like a fixture.
    root = tempfile.mkdtemp(prefix="relay-selftest-fix-")
    p = selftest.write_fixture("unread_x", txt, root=root)
    check("write_fixture creates the file", os.path.isfile(p))
    with open(p) as fh:
        loaded = [l for l in fh.read().splitlines()
                  if l.strip() and not l.startswith("#")]
    check("a captured fixture round-trips through the corpus loader",
          loaded == body)

    # --- the report line -------------------------------------------------
    shell = selftest.report_line("some-tab", "zsh", True, "idle", "", set())
    check("a shell tab is reported as out of scope, not as a failure",
          "skipped" in shell and "shell" in shell and "CANNOT READ" not in shell)
    # Relay's OWN panel draws box-rule chrome and is not Claude. Counting it
    # would raise a false alarm, and with --capture would write a junk frame
    # into the corpus that then fails the suite forever.
    own = selftest.report_line("RELAY CONSOLE", "python3", False, "idle", "",
                               set())
    check("relay's own panel is skipped, not reported as unreadable",
          "skipped" in own and "CANNOT READ" not in own)
    check("a skipped tab names the job, so a packaging change is visible "
          "rather than silent", "python3" in own)
    check("a real Claude tab runs as node and IS in scope",
          selftest.looks_like_claude("node")
          and not selftest.looks_like_claude("zsh")
          and not selftest.looks_like_claude("python3")
          and not selftest.looks_like_claude(""))
    skipped = "\n".join(selftest.summarise(7, 5, 0, [], n_skipped=2))
    check("skipped tabs are counted in the summary, never silently dropped",
          "2 tab(s) skipped" in skipped)
    bad = selftest.report_line("api", "claude", False, "idle", "", set())
    check("an unreadable tab is called out by name", "CANNOT READ" in bad
          and "api" in bad)
    good = selftest.report_line("api", "claude", True, "working",
                                "no actionable prompt", {"box", "working"})
    check("a readable tab reports its state", "working" in good)
    check("a readable tab reports WHICH anchors matched - 'it broke' without "
          "'which part' is one bit of information",
          "box" in good and "working" in good)
    check("a readable tab reports what relay would decide, without doing it",
          "no actionable prompt" in good)

    # --- the summary is the part that decides whether this gets run twice
    none_running = "\n".join(selftest.summarise(4, 0, 0, []))
    check("with no Claude tab, the run says it verified nothing rather than "
          "printing a green tick", "nothing to verify" in none_running)
    check("and it does not claim success", "✓ relay can read all"
          not in none_running)
    allgood = "\n".join(selftest.summarise(5, 3, 0, []))
    check("all-readable reports the count it actually checked",
          "all 3 Claude tab" in allgood)
    unread = "\n".join(selftest.summarise(5, 3, 2, []))
    check("unreadable tabs are counted against the total", "2 of 3" in unread)
    check("without --capture, the operator is told how to capture",
          "--capture" in unread)
    withcap = "\n".join(selftest.summarise(5, 3, 1, ["/tmp/f.txt"]))
    check("with --capture, the paths are printed", "/tmp/f.txt" in withcap)
    check("and the NEXT command is spelled out - knowing what to do next was "
          "itself the work that kept the manual gates unrun",
          "test_state.py" in withcap)
    check("and it warns the capture may be a false positive",
          "not actually running Claude" in withcap)

    print()
    if _fails:
        print(f"{len(_fails)} FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
