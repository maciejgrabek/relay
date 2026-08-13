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

    # --- context window inference ----------------------------------------
    # The 1M variants report the same model string as the 200k ones, so the
    # window cannot be read off the transcript.
    check("a small session is measured against the 200k window",
          u["window"] == 200_000)
    usage.reset_cache()
    _write("-p", "cccc-3333", [_assistant(out=1, cache_read=303_591)])
    u2 = usage.read("cccc-3333")
    check("a session seen above 200k is measured against the 1M window - it "
          "is demonstrably not a 200k model", u2["window"] == 1_000_000)
    check("and its percentage is computed against that wider window",
          u2["pct"] == 30)
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

    # --- rendering ---------------------------------------------------------
    check("fmt_tokens keeps small counts exact", usage.fmt_tokens(812) == "812")
    check("fmt_tokens abbreviates thousands", usage.fmt_tokens(48_200) == "48.2k")
    check("fmt_tokens drops a trailing .0", usage.fmt_tokens(1000) == "1k")
    check("fmt_tokens abbreviates millions",
          usage.fmt_tokens(1_200_000) == "1.2M")

    check("the CTX cell is a bare percentage",
          usage.ctx_cell({"pct": 62}) == "62%")
    # The whole point of the placement decision: no data must render as an
    # EMPTY cell, never a zero. A tab relay cannot read is not a tab at 0%.
    check("no data renders as an empty cell, not 0%", usage.ctx_cell(None) == "")
    check("no data has no color band", usage.ctx_level(None) == "")
    check("a low percentage is quiet", usage.ctx_level({"pct": 20}) == "ok")
    check("a high percentage warns", usage.ctx_level({"pct": 80}) == "warn")
    check("a nearly full context is loud", usage.ctx_level({"pct": 95}) == "high")
    check("the bands do not overlap or leave a gap",
          usage.ctx_level({"pct": usage.CTX_WARN}) == "warn"
          and usage.ctx_level({"pct": usage.CTX_WARN - 1}) == "ok"
          and usage.ctx_level({"pct": usage.CTX_HIGH}) == "high")

    lines = usage.preview_lines(
        {"pct": 62, "ctx": 124_000, "window": 200_000, "out": 48_200,
         "in": 3100, "cached": 1_200_000, "turns": 41,
         "model": "claude-opus-5"}, registered=True)
    body = "\n".join(lines)
    check("the preview leads with the number the operator acts on",
          "62% of context" in body and "124k/200k" in body)
    check("the preview breaks the total down", "out 48.2k" in body
          and "in 3.1k" in body and "cached 1.2M" in body)
    check("the preview names the model and turn count",
          "41 turns" in body and "claude-opus-5" in body)
    # Cache reads are repeat billing on the same prompt. A "total tokens" that
    # folded them in would be a number that measures nothing.
    check("cached tokens are reported separately, never folded into a total",
          "1.2M" in body and "1.25M" not in body and "total" not in body.lower())

    unreg = "\n".join(usage.preview_lines(None, registered=False))
    check("an unregistered tab is told WHY there is no number, not just left "
          "blank", "not registered" in unreg)
    check("and it is told what to do about it", "relay join" in unreg)
    none_yet = "\n".join(usage.preview_lines(None, registered=True,
                                             has_id=True))
    check("a registered session with no transcript yet says so distinctly",
          "no transcript" in none_yet and "not registered" not in none_yet)
    # The case that shipped broken: EVERY session in an existing swarm has an
    # empty claude_session_id the first time it runs a build with usage in it,
    # because the rows predate the column. Reporting that as "no transcript
    # yet" tells the operator to wait for something that never arrives.
    no_id = "\n".join(usage.preview_lines(None, registered=True,
                                          has_id=False))
    check("a session registered before ids were recorded is told to re-join",
          "relay join" in no_id)
    check("and it is NOT reported as merely waiting for a first turn",
          "no transcript" not in no_id)
    check("and it is told re-joining is not destructive - a rename would lose "
          "every peer that knows the name",
          "keeps the name" in no_id)
    check("the three no-number states are all distinguishable",
          len({no_id, none_yet,
               "\n".join(usage.preview_lines(None, registered=False))}) == 3)

    print()
    if _fails:
        print(f"{len(_fails)} FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
