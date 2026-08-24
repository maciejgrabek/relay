"""Tests for pure recap aggregation. No iTerm2, no file I/O.

Run: python3 iterm/test_recap.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import recap  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    entries = [
        {"ts": 100.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "escalated"},
        {"ts": 200.0, "verdict": "delivered"},
        {"ts": 50.0,  "verdict": "auto-approved"},   # before window
        {"ts": 160.0, "verdict": "would-approve"},   # dry-run, not counted
        {"bogus": 1},                                # garbled, skipped
    ]
    s = recap.summarize(entries, since=100.0)
    ok &= check("cleared counts auto-approved in window", s["cleared"] == 2)
    ok &= check("woke counts escalated", s["woke"] == 1)
    ok &= check("delivered counts delivered", s["delivered"] == 1)

    empty = recap.summarize([], since=0.0)
    ok &= check("empty log -> zeros",
                empty == {"cleared": 0, "woke": 0, "delivered": 0})
    ok &= check("start_of_today is a float epoch",
                isinstance(recap.start_of_today(), float)
                and recap.start_of_today() > 0)

    ok &= _review(check)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _review(check) -> bool:
    """The review loop: separating approvals the SAFETY GATE made from those
    the ARM LEVEL made over its objection. Both land in the log as
    'auto-approved' and they mean opposite things - collapsing them is what
    made 135 overridden dangerous commands invisible for months."""
    ok = True

    def ap(reason, cmd="", sess="w1", ts=1000.0):
        return {"ts": ts, "verdict": "auto-approved", "reason": reason,
                "command": cmd, "session": sess}

    entries = [
        ap("safe permission prompt", "ls -la"),
        ap("safe permission prompt", "grep -rn TODO ."),
        ap("insane-approve (dangerous command)", "ssh root@box 'systemctl restart x'"),
        ap("wild-approve (dangerous command)", "rm -rf /tmp/build"),
        ap("extreme-approve (dangerous command)", "psql -c 'drop table t'", "w2"),
        ap("insane-approve (command too large to verify (header off-screen) - fail safe)"),
        ap("insane-approve (cursor not on option 1 - fail safe)", "curl -X POST h"),
        {"ts": 1000.0, "verdict": "escalated", "reason": "dangerous command",
         "command": "dd if=/dev/zero", "session": "w1"},
        {"ts": 1000.0, "verdict": "delivered", "reason": "", "command": "",
         "session": "w1"},
        {"ts": 1000.0, "verdict": "extreme-pushed", "reason": "", "command": "",
         "session": "w2"},
        {"ts": 1.0, "verdict": "auto-approved", "reason": "safe permission "
         "prompt", "command": "old", "session": "w1"},   # before the window
    ]
    r = recap.review(entries, since=100.0)
    ok &= check("review counts every auto-approval in the window",
                r["approved"] == 7)
    ok &= check("review excludes entries before the window",
                r["approved"] == 7 and r["clean"] == 2)
    ok &= check("an approval the safety gate made is 'clean'", r["clean"] == 2)
    ok &= check("wild/insane/extreme approvals over a DANGEROUS verdict are "
                "counted as overridden, whatever the arm level",
                r["overridden"] == 3)
    ok &= check("approvals the gate could not read are counted separately - "
                "unexamined is a different risk from overruled",
                r["unverified"] == 2)
    ok &= check("the three approval kinds account for every approval",
                r["clean"] + r["overridden"] + r["unverified"] == r["approved"])
    ok &= check("an ESCALATION is never counted as an approval, even when its "
                "reason is 'dangerous command'",
                r["escalated"] == 1 and r["overridden"] == 3)
    ok &= check("deliveries and extreme pushes are counted, not conflated "
                "with approvals", r["delivered"] == 1 and r["pushed"] == 1)

    # Grouping by exact command produces a bucket of "1x" per one-off and
    # answers nothing; the verb is what tells the operator what they authorised.
    ok &= check("overrides are grouped by the risky verb",
                r["override_cmds"].get("ssh") == 1
                and r["override_cmds"].get("rm -rf") == 1
                and r["override_cmds"].get("psql") == 1)
    ok &= check("a command matching several risky verbs counts under each - "
                "`ssh ... | psql` is honestly both",
                set(recap.risk_tags("ssh h 'psql -c drop'")) >= {"ssh", "psql"})
    ok &= check("a dangerous command this summary cannot label is bucketed as "
                "'(other)', never dropped - dropping it would understate the "
                "total the headline count reports",
                recap.risk_tags("some-exotic-thing --wipe") == ["(other)"])
    ok &= check("an unreadable command is labelled as such",
                recap.risk_tags("") == ["(unreadable)"]
                and r["unverified_cmds"].get("(unreadable)") == 1)
    ok &= check("overrides are attributed to the session that made them",
                r["sessions"].get("w1", 0) >= 2 and "w2" in r["sessions"])

    body = "\n".join(recap.review_lines(r))
    ok &= check("the report leads with the split, not a bare total",
                "cleared by the safety gate" in body and "approved over it" in body)
    ok &= check("the report gives a RATE as well as a count - a count alone "
                "reads as alarming or complacent depending on the denominator",
                "% of approvals" in body)
    ok &= check("the report names the overridden verbs", "ssh" in body)
    ok &= check("the report attributes them to sessions", "by session" in body)

    # --- escalations are not one thing ------------------------------------
    # The bug this pins, found by running the verb against a real log: 114 of
    # 114 escalations were sessions asking questions, and the report said only
    # "escalated to you: 114" - which reads as "the gate refused 114 commands"
    # and would send an operator off to loosen a gate that had never fired.
    def esc(reason, cmd="", sess="w1", ts=1000.0):
        return {"ts": ts, "verdict": "escalated", "reason": reason,
                "command": cmd, "session": sess}

    mixed = recap.review([
        esc("real question - hands off"),
        esc("real question - hands off"),
        esc("dangerous command", "ssh root@box"),
        esc("dangerous command", "rm -rf /var/lib"),
        esc("could not parse command - fail safe", "???"),
        esc("cursor not on option 1 - fail safe", "curl -X POST h"),
        esc("some future reason nobody has written yet", "make build"),
    ], since=0.0)
    k = mixed["esc_kinds"]
    ok &= check("a question and a refused command are counted separately",
                k["question"] == 2 and k["dangerous"] == 2)
    ok &= check("an escalation the gate could not read is its own class",
                k["unsure"] == 2)
    ok &= check("an escalation reason this build does not know is bucketed, "
                "never dropped - the classes must add up to the total",
                k["other"] == 1
                and sum(k.values()) == mixed["escalated"] == 7)
    ok &= check("refused commands are grouped by the risky verb, like "
                "overrides are - one vocabulary for both outcomes",
                mixed["esc_cmds"].get("ssh") == 1
                and mixed["esc_cmds"].get("rm -rf") == 1)
    mixed_body = "\n".join(recap.review_lines(mixed))
    ok &= check("the report names each escalation class rather than printing "
                "one total", "a session asked you something" in mixed_body
                and "the gate refused a command" in mixed_body)
    ok &= check("a refused command is reported as the gate STANDING, not as "
                "an override", "it STOOD" in mixed_body)

    questions_only = recap.review([esc("real question - hands off")] * 3,
                                  since=0.0)
    q_body = "\n".join(recap.review_lines(questions_only))
    ok &= check("with no command ever refused, the report says so - that is "
                "the finding, and it is invisible in a bare total",
                "did not refuse a single command" in q_body)
    ok &= check("...and does not print an empty refused-command block",
                "it STOOD" not in q_body)

    # --- a rate keeps its denominator -------------------------------------
    # 6.2% of 16 approvals is one ssh. A percentage without its sample size is
    # how a handful of rows gets read as a trend.
    small = recap.review([ap("safe permission prompt", "ls")] * 3
                         + [ap("insane-approve (dangerous command)", "ssh h")],
                         since=0.0)
    small_body = "\n".join(recap.review_lines(small))
    ok &= check("the rate is printed with the counts it came from",
                "% of approvals" in small_body and "(1 of 4)" in small_body)

    # --- the window the rows actually cover -------------------------------
    # `--all` cannot mean all time: the audit log is pruned at every TUI start,
    # so the report states the range it really saw.
    spanned = recap.review([ap("safe permission prompt", "ls", ts=1_000_000.0),
                            ap("safe permission prompt", "ls", ts=9_000_000.0)],
                           since=0.0)
    ok &= check("review records the first and last timestamp it saw",
                spanned["first_ts"] == 1_000_000.0
                and spanned["last_ts"] == 9_000_000.0)
    ok &= check("the report opens with the range it actually covers",
                "covering" in "\n".join(recap.review_lines(spanned)))
    ok &= check("an empty window claims no coverage at all",
                "covering" not in "\n".join(
                    recap.review_lines(recap.review([], since=0.0))))

    clean_only = recap.review([ap("safe permission prompt", "ls")], since=0.0)
    clean_body = "\n".join(recap.review_lines(clean_only))
    ok &= check("with nothing waved through, the report says so plainly "
                "instead of printing an empty warning block",
                "Nothing was waved through" in clean_body
                and "DANGEROUS" not in clean_body)
    ok &= check("an empty log reviews to zeros rather than raising",
                recap.review([], since=0.0)["approved"] == 0)
    ok &= check("a garbled row is skipped, not fatal",
                recap.review([{"ts": "nope"}, {"verdict": None},
                              ap("safe permission prompt", "ls")],
                             since=0.0)["approved"] == 1)
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
