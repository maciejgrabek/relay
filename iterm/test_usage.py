"""Token usage tests: transcript resolution, incremental reads, and the two
numbers the panel actually shows.

Run: python3 iterm/test_usage.py

Every fixture is a hand-written .jsonl under a temp projects root, so nothing
here reads the developer's real ~/.claude/projects.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = tempfile.mkdtemp(prefix="relay-test-projects-")
os.environ["RELAY_CLAUDE_PROJECTS"] = ROOT
SESS_ROOT = tempfile.mkdtemp(prefix="relay-test-sessions-")
os.environ["RELAY_CLAUDE_SESSIONS"] = SESS_ROOT
# The settings file names the context window. Pointed at a temp path that does
# NOT exist yet, so the default case under test is "no configured model" and
# the developer's own ~/.claude/settings.json can never leak into a result.
SETT = os.path.join(tempfile.mkdtemp(prefix="relay-test-settings-"),
                    "settings.json")
os.environ["RELAY_CLAUDE_SETTINGS"] = SETT

import usage  # noqa: E402

_fails = []


def check(label, cond):
    print(("  OK   " if cond else " FAIL  ") + label)
    if not cond:
        _fails.append(label)
    return cond


def _assistant(out=0, inp=0, cache_read=0, cache_create=0, model="claude-opus-5"):
    return json.dumps({"type": "assistant", "message": {
        "model": model,
        "usage": {"output_tokens": out, "input_tokens": inp,
                  "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": cache_create}}})


def _write(project, sid, lines, mode="w"):
    d = os.path.join(ROOT, project)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    with open(p, mode) as fh:
        for line in lines:
            fh.write(line + "\n")
    return p


def main():
    usage.reset_cache()

    # --- resolution: a UUID is globally unique, so the project directory is
    # found rather than derived from a cwd slug. A slug rebuild would be a
    # second source of truth that drifts the moment the slug rules change.
    p = _write("-Users-me-Work-thing", "aaaa-1111", [_assistant(out=10)])
    check("transcript_path finds the file in whatever project dir holds it",
          usage.transcript_path("aaaa-1111") == p)
    check("an unknown session id resolves to nothing",
          usage.transcript_path("nope-9999") is None)
    check("an empty session id resolves to nothing - an unregistered tab must "
          "never accidentally match a real transcript",
          usage.transcript_path("") is None)
    # The id comes out of the DB, written by a CLI process. Path syntax in it
    # must not be able to walk out of the projects root through the glob.
    check("a session id containing a path separator is refused",
          usage.transcript_path("../../etc/passwd") is None
          and usage.transcript_path("a/b") is None)

    # --- the two numbers -------------------------------------------------
    usage.reset_cache()
    _write("-p", "bbbb-2222", [
        _assistant(out=100, inp=5, cache_read=1000, cache_create=50),
        _assistant(out=200, inp=7, cache_read=3000, cache_create=20)])
    u = usage.read("bbbb-2222")
    check("output tokens accumulate across turns", u["out"] == 300)
    check("real input tokens accumulate", u["in"] == 12)
    check("cache reads accumulate", u["cached"] == 4000)
    check("turns are counted", u["turns"] == 2)
    # ctx is a LEVEL, not a sum: it is the prompt size of the last turn, which
    # is what a compaction resets. Summing it would climb forever while the
    # real context sat still.
    check("context is the LAST turn's prompt, not a running sum",
          u["ctx"] == 7 + 3000 + 20)
    check("the model is recorded", u["model"] == "claude-opus-5")

    # --- the context window -------------------------------------------------
    # The 1M variants report the SAME model string as the 200k ones, so the
    # window is never in the transcript. Three ways it can be settled, and the
    # difference between them is the whole correctness story here.
    check("with no configured model and nothing observed, the window is only "
          "ASSUMED", u["window"] == 200_000
          and u["window_source"] == "assumed" and u["window_known"] is False)

    # 1. OBSERVED. A reading above 200k is proof: that session is not a 200k
    #    model. This is the only route that needs no outside information.
    usage.reset_cache()
    _write("-p", "cccc-3333", [_assistant(out=1, cache_read=303_591)])
    u2 = usage.read("cccc-3333")
    check("a session seen above 200k is measured against the 1M window - it "
          "is demonstrably not a 200k model", u2["window"] == 1_000_000)
    check("and that window is KNOWN, not assumed - it was demonstrated",
          u2["window_source"] == "observed" and u2["window_known"] is True)
    check("and its percentage is computed against that wider window",
          u2["pct"] == 30)

    # 2. CONFIGURED. The `[1m]` suffix is stripped from the transcript but
    #    survives in the settings file, which is where the operator chose it.
    check("no settings file means no configured window",
          usage.configured_window() == 0)
    with open(SETT, "w") as fh:
        json.dump({"model": "claude-opus-5[1m]"}, fh)
    usage.reset_cache()
    check("a `[1m]` model in settings names the 1M window",
          usage.configured_window() == 1_000_000)
    _write("-p", "wwww-1m11", [_assistant(out=1, cache_read=48_063)])
    u1m = usage.read("wwww-1m11")
    check("a session under 200k on a configured 1M model is measured against "
          "1M, not against the 200k default",
          u1m["window"] == 1_000_000 and u1m["pct"] == 5)
    check("a configured window counts as known - the operator named it",
          u1m["window_source"] == "config" and u1m["window_known"] is True)
    with open(SETT, "w") as fh:
        json.dump({"model": "claude-opus-5"}, fh)
    usage.reset_cache()
    check("a model with no `[1m]` suffix names the 200k window",
          usage.configured_window() == 200_000)
    # An in-session /model switch is invisible to relay, so the observed route
    # must still be able to overrule the configured one - in the safe
    # direction only (a reading is proof; the settings file is a default).
    _write("-p", "xxxx-obs1", [_assistant(out=1, cache_read=303_591)])
    check("an observed reading above 200k overrules a 200k settings file",
          usage.read("xxxx-obs1")["window_source"] == "observed")
    with open(SETT, "w") as fh:
        fh.write("{ not json")
    usage.reset_cache()
    check("a malformed settings file names nothing, it does not crash",
          usage.configured_window() == 0)
    os.remove(SETT)
    usage.reset_cache()

    # 3. ASSUMED - and this is the regression that prompted the rewrite.
    #    A live 1M session sitting at 187,021 tokens (18.7% full) was rendered
    #    as 94% in DANGER red, because the old code divided by 200k on no
    #    evidence at all. A healthy session painted as about to die is worse
    #    than no cell: it trains the operator to ignore the column.
    _write("-p", "yyyy-1870", [_assistant(out=1, cache_read=187_021)])
    ubad = usage.read("yyyy-1870")
    check("a big reading on an UNKNOWN window is not reported as a "
          "percentage of 200k", usage.ctx_cell(ubad) == "187k")
    check("and it raises no alarm, because there is nothing to be alarmed "
          "about yet", usage.ctx_level(ubad) == "")

    usage.reset_cache()
    _write("-p", "dddd-4444", [_assistant(out=1, cache_read=150_000)])
    check("a percentage is a whole number of the window",
          usage.read("dddd-4444")["pct"] == 75)

    # A context reading beyond the window must not render as more than full.
    usage.reset_cache()
    _write("-p", "eeee-5555", [_assistant(out=1, cache_read=1_400_000)])
    check("pct is clamped at 100 - a bar past full reads as a bug, not a "
          "warning", usage.read("eeee-5555")["pct"] == 100)

    # --- incremental reads -------------------------------------------------
    # A transcript reaches 4.7MB and 1400+ messages in a day. Re-parsing that
    # per session per refresh tick would cost more than the rest of the panel.
    usage.reset_cache()
    _write("-p", "ffff-6666", [_assistant(out=10)])
    usage.read("ffff-6666")
    off1 = usage._STATE[usage.transcript_path("ffff-6666")]["offset"]
    check("the first read stores a byte offset", off1 > 0)
    _write("-p", "ffff-6666", [_assistant(out=90)], mode="a")
    u3 = usage.read("ffff-6666")
    check("an appended turn is added to the running total", u3["out"] == 100)
    check("only the appended bytes were parsed",
          usage._STATE[usage.transcript_path("ffff-6666")]["offset"] > off1)
    check("re-reading with nothing appended changes nothing",
          usage.read("ffff-6666")["out"] == 100)

    # A half-written final line is Claude Code mid-write. It must be left for
    # the next read, not parsed in half and skipped forever.
    path = usage.transcript_path("ffff-6666")
    with open(path, "a") as fh:
        fh.write('{"type":"assistant","message":{"usage":{"output_toke')
    check("a partial trailing line is not counted", usage.read("ffff-6666")["out"] == 100)
    with open(path, "a") as fh:
        fh.write('ns":7}}}\n')
    check("and it is counted once it is complete",
          usage.read("ffff-6666")["out"] == 107)

    # A file that shrank was replaced or truncated, so the bytes already
    # counted are gone - keeping the totals would add new numbers onto stale.
    _write("-p", "ffff-6666", [_assistant(out=5)])
    check("a truncated transcript restarts the totals rather than adding to "
          "stale ones", usage.read("ffff-6666")["out"] == 5)

    # --- degradation: never a wrong number ---------------------------------
    usage.reset_cache()
    check("no session id reads as no data, not as zero tokens",
          usage.read("") is None)
    check("a missing transcript reads as no data", usage.read("gone-0000") is None)
    _write("-p", "hhhh-7777", ['{"type":"user","message":{"role":"user"}}'])
    check("a transcript with no assistant turns yet reads as no data - a "
          "registered session that has not run is not a session at 0%",
          usage.read("hhhh-7777") is None)
    _write("-p", "iiii-8888", ["not json at all", _assistant(out=42)])
    check("an unparseable line is skipped, not fatal",
          usage.read("iiii-8888")["out"] == 42)
    # A session can register before its first turn, so the file appears later.
    # A negative cache would hide it for the rest of the panel's life.
    usage.reset_cache()
    check("a not-yet-existing transcript reads as no data",
          usage.read("jjjj-9999") is None)
    _write("-p", "jjjj-9999", [_assistant(out=3)])
    check("and it is picked up once it appears - a miss is never cached",
          usage.read("jjjj-9999")["out"] == 3)

    # --- resolving a tab to a session WITHOUT registration -----------------
    # Claude Code writes ~/.claude/sessions/<pid>.json for every running
    # session. Walking up from the tab's foreground job finds the id with no
    # `relay join` at all - which is what lets an unregistered tab report
    # usage, and what keeps a tab honest after it restarts Claude (the DB
    # would still name the PREVIOUS run's transcript and show its numbers).
    usage.reset_cache()
    os.makedirs(SESS_ROOT, exist_ok=True)
    with open(os.path.join(SESS_ROOT, "4242.json"), "w") as fh:
        json.dump({"pid": 4242, "sessionId": "live-abcd",
                   "cwd": "/w", "status": "idle"}, fh)
    check("a claude pid resolves straight to its session id",
          usage.session_id_for_pid(4242) == "live-abcd")

    # iTerm2 reports the FOREGROUND job's pid, which for a Claude tab is often
    # a descendant (an MCP server, a running Bash tool). Measured live: iTerm2
    # said 92157 = chrome-devtools-mcp, whose grandparent 92030 was `claude`.
    chain = {9001: 9002, 9002: 4242}
    real_parent = usage._parent_pid
    usage._parent_pid = lambda p: chain.get(p, 0)
    try:
        usage.reset_cache()
        check("a descendant pid walks up to the owning claude session",
              usage.session_id_for_pid(9001) == "live-abcd")
        usage.reset_cache()
        check("an unrelated process tree resolves to nothing, not to whichever "
              "session happens to be running",
              usage.session_id_for_pid(7777) == "")
        # An unbounded walk would turn one shell tab into an endless ps chain.
        usage.reset_cache()
        loop = {i: i + 1 for i in range(100, 200)}
        usage._parent_pid = lambda p: loop.get(p, 0)
        check("the walk is bounded, so a deep tree cannot hang the panel",
              usage.session_id_for_pid(100) == "")
    finally:
        usage._parent_pid = real_parent
    check("a missing or nonsense pid resolves to nothing",
          usage.session_id_for_pid(0) == ""
          and usage.session_id_for_pid(None) == ""
          and usage.session_id_for_pid("nope") == "")
    # A session that starts after the index was built must be picked up: the
    # index is keyed on the sessions-dir mtime, not cached forever.
    usage.reset_cache()
    usage.session_id_for_pid(4242)
    with open(os.path.join(SESS_ROOT, "4343.json"), "w") as fh:
        json.dump({"pid": 4343, "sessionId": "later-efgh"}, fh)
    os.utime(SESS_ROOT, (0, 0))    # force a different mtime
    check("a session that starts after the first lookup is still found",
          usage.session_id_for_pid(4343) == "later-efgh")
    # A malformed or partial file must not take the whole index down - Claude
    # Code writes these live and one can be caught mid-write.
    with open(os.path.join(SESS_ROOT, "bad.json"), "w") as fh:
        fh.write("{not json")
    os.utime(SESS_ROOT, (1, 1))
    check("a malformed session file is skipped, not fatal",
          usage.session_id_for_pid(4242) == "live-abcd")

    # --- rendering ---------------------------------------------------------
    check("fmt_tokens keeps small counts exact", usage.fmt_tokens(812) == "812")
    check("fmt_tokens abbreviates thousands", usage.fmt_tokens(48_200) == "48.2k")
    check("fmt_tokens drops a trailing .0", usage.fmt_tokens(1000) == "1k")
    check("fmt_tokens abbreviates millions",
          usage.fmt_tokens(1_200_000) == "1.2M")

    def _u(**kw):
        d = {"pct": 62, "ctx": 124_000, "window": 200_000,
             "window_source": "observed", "window_known": True,
             "out": 48_200, "in": 3100, "cached": 1_200_000, "turns": 41,
             "model": "claude-opus-5"}
        d.update(kw)
        return d

    check("the CTX cell is a bare percentage when the window is known",
          usage.ctx_cell(_u()) == "62%")
    # When the window is only assumed, the percentage would be an invented
    # denominator on a panel whose whole argument is that it tells the truth.
    # The token level is a number relay actually read off disk.
    check("an unknown window renders the LEVEL, not a made-up percentage",
          usage.ctx_cell(_u(window_known=False, window_source="assumed",
                            ctx=48_063)) == "48.1k")
    check("and it carries no color band, because there is no threshold to be "
          "past without a window",
          usage.ctx_level(_u(window_known=False, pct=95)) == "")
    # The whole point of the placement decision: no data must render as an
    # EMPTY cell, never a zero. A tab relay cannot read is not a tab at 0%.
    check("no data renders as an empty cell, not 0%", usage.ctx_cell(None) == "")
    check("no data has no color band", usage.ctx_level(None) == "")
    check("a low percentage is quiet", usage.ctx_level(_u(pct=20)) == "ok")
    check("a high percentage warns", usage.ctx_level(_u(pct=80)) == "warn")
    check("a nearly full context is loud", usage.ctx_level(_u(pct=95)) == "high")
    check("the bands do not overlap or leave a gap",
          usage.ctx_level(_u(pct=usage.CTX_WARN)) == "warn"
          and usage.ctx_level(_u(pct=usage.CTX_WARN - 1)) == "ok"
          and usage.ctx_level(_u(pct=usage.CTX_HIGH)) == "high")

    lines = usage.preview_lines(_u(), registered=True)
    body = "\n".join(lines)
    check("the preview leads with the number the operator acts on",
          "62% of context" in body and "124k/200k" in body)
    # ctx is the prompt size of the LAST completed turn: the turn in flight,
    # and every tool result since, are not in it yet.
    check("the preview says the level is as of the last turn, not live",
          "last turn" in body)
    check("an observed window is stated plainly, with no hedge - it was "
          "demonstrated", "settings" not in body and "unknown" not in body)

    cfg = "\n".join(usage.preview_lines(
        _u(window_source="config", window=1_000_000, pct=5), registered=True))
    check("a window taken from settings says so, so the operator knows which "
          "number is read and which is assumed", "from settings" in cfg)

    noscale = "\n".join(usage.preview_lines(
        _u(window_known=False, window_source="assumed", ctx=48_063),
        registered=True))
    check("an unknown window reports the level and names the window as the "
          "unknown", "48.1k" in noscale and "window unknown" in noscale)
    check("it never prints a percentage it cannot stand behind",
          "%" not in noscale)
    check("and it says exactly what would fix it",
          "settings.json" in noscale and "model" in noscale)
    check("the breakdown is still there - an unknown window costs the "
          "percentage, not the whole block",
          "out 48.2k" in noscale and "41 turns" in noscale)
    check("the preview breaks the total down", "out 48.2k" in body
          and "in 3.1k" in body and "cached 1.2M" in body)
    check("the preview names the model and turn count",
          "41 turns" in body and "claude-opus-5" in body)
    # Cache reads are repeat billing on the same prompt. A "total tokens" that
    # folded them in would be a number that measures nothing.
    check("cached tokens are reported separately, never folded into a total",
          "1.2M" in body and "1.25M" not in body and "total" not in body.lower())

    # Registration is no longer required: the process-tree lookup resolves an
    # unregistered tab the same as a registered one, so an unregistered tab
    # with a resolved id must be treated identically.
    unreg = "\n".join(usage.preview_lines(
        _u(pct=5, ctx=10_000), registered=False))
    check("an UNREGISTERED tab with a resolved id reports numbers like any "
          "other - registration is no longer the gate",
          "5% of context" in unreg and "relay join" not in unreg)
    none_yet = "\n".join(usage.preview_lines(None, registered=True,
                                             has_id=True))
    check("a registered session with no transcript yet says so distinctly",
          "no transcript" in none_yet and "not registered" not in none_yet)
    no_id = "\n".join(usage.preview_lines(None, registered=True,
                                          has_id=False))
    check("a tab with no resolvable session id names the LIKELY cause first",
          "not running here" in no_id)
    check("and it is NOT reported as merely waiting for a first turn",
          "no transcript" not in no_id)
    check("relay join survives as the explicit fallback, not the headline",
          "relay join" in no_id)
    check("the two no-number states stay distinguishable",
          no_id != none_yet)

    print()
    if _fails:
        print(f"{len(_fails)} FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
