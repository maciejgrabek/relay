"""Tests for the desktop-widget publisher. No iTerm2/Textual/sqlite imports.

Run: python3 iterm/test_widget.py    or    ./test/run.sh
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import widget  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


# A mascot block shaped like the real one: app._speech_bubble renders the text
# row as "<attach> <text> |", which is where the phrase has to come from.
ART = [
    "     ((*))",
    "    /-----\\   /------------------\\",
    "    | . . |  \u25c3 7 cleared, quiet. \u2502",
    "    |  _  |   \\------------------/",
    "   /-------\\",
]


def run():
    ok = True

    # --- phrase extraction -------------------------------------------------
    # Compact mode has no creature to read, so the sentence must survive as its
    # own field. If this regresses, compact mode silently goes blank.
    ok &= check("speech() pulls the bubble text",
                widget.speech(ART) == "7 cleared, quiet.")
    ok &= check("speech() on art with no bubble -> empty",
                widget.speech(["  no bubble here  "]) == "")
    ok &= check("speech() on empty art -> empty", widget.speech([]) == "")

    # --- payload -----------------------------------------------------------
    p = widget.payload("guarding", "#2fc866", ART, armed=3, awaiting=0,
                       working=False, paused=False, band="calm", sessions=7,
                       panel_sid="ABC-123", now=1000.0)
    ok &= check("payload carries ts", p["ts"] == 1000.0)
    ok &= check("payload carries state/color",
                p["state"] == "guarding" and p["color"] == "#2fc866")
    ok &= check("payload carries art verbatim", p["art"] == ART)
    ok &= check("payload derives phrase", p["phrase"] == "7 cleared, quiet.")
    ok &= check("payload carries counts",
                p["armed"] == 3 and p["awaiting"] == 0 and p["sessions"] == 7)
    ok &= check("payload carries panel_sid for the focus button",
                p["panel_sid"] == "ABC-123")
    ok &= check("payload carries attention_sid (None when nothing waits)",
                "attention_sid" in p and p["attention_sid"] is None)

    p3 = widget.payload("alarmed", "#f55", ART, armed=1, awaiting=2,
                        working=False, paused=False, band="calm", sessions=2,
                        panel_sid="PANEL", attention_sid="NEEDS-ME", now=1.0)
    ok &= check("payload carries the session that needs a human",
                p3["attention_sid"] == "NEEDS-ME")

    # The widget renders `art` into a <pre>. Textual markup leaking through
    # would show up as literal "[bold]" text in the creature.
    p2 = widget.payload("idle", "#888", ["[bold]hi[/]", "  ((*))"],
                        armed=0, awaiting=0, working=False, paused=False,
                        band="calm", sessions=0, now=1.0)
    ok &= check("payload strips Textual markup from art",
                p2["art"] == ["hi", "  ((*))"])

    # JSON-serialisable, or write_state raises at runtime inside the TUI's
    # render loop - the worst possible place to find out.
    try:
        json.dumps(p)
        ok &= check("payload is JSON-serialisable", True)
    except TypeError:
        ok &= check("payload is JSON-serialisable", False)

    # --- write / read / freshness ------------------------------------------
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sub", "widget.json")     # dir does not exist yet

    widget.write_state(p, path=path)
    ok &= check("write_state creates missing dirs", os.path.exists(path))
    with open(path) as f:
        ok &= check("written file round-trips", json.load(f)["state"] == "guarding")
    ok &= check("write_state leaves no .tmp behind",
                not os.path.exists(path + ".tmp"))

    ok &= check("fresh right after a write",
                widget.state_fresh(now=1000.0 + 1, path=path))
    ok &= check("stale past the window",
                not widget.state_fresh(now=1000.0 + widget.STALE_S + 1, path=path))
    ok &= check("stale exactly at the boundary is still fresh",
                widget.state_fresh(now=1000.0 + widget.STALE_S, path=path))

    # Overwriting must replace, not append or corrupt.
    widget.write_state(widget.payload("alarmed", "#ff5555", ART, armed=1,
                                      awaiting=2, working=False, paused=False,
                                      band="calm", sessions=2, now=2000.0),
                       path=path)
    with open(path) as f:
        ok &= check("overwrite replaces cleanly", json.load(f)["state"] == "alarmed")

    # --- clear -------------------------------------------------------------
    # On quit relay removes the file so the widget greys out immediately rather
    # than waiting out the staleness window.
    widget.clear_state(path=path)
    ok &= check("clear_state removes the file", not os.path.exists(path))
    try:
        widget.clear_state(path=path)
        ok &= check("clear_state on a missing file does not raise", True)
    except Exception:
        ok &= check("clear_state on a missing file does not raise", False)

    ok &= check("missing file is not fresh",
                not widget.state_fresh(now=1000.0, path=path))

    # Garbled state must read as "relay is off", never crash the caller.
    with open(path, "w") as f:
        f.write("{not json")
    ok &= check("garbled file is not fresh",
                not widget.state_fresh(now=1000.0, path=path))

    # --- path override -----------------------------------------------------
    old = os.environ.get("RELAY_WIDGET_STATE")
    os.environ["RELAY_WIDGET_STATE"] = "/tmp/relay-widget-test.json"
    try:
        ok &= check("RELAY_WIDGET_STATE overrides the path",
                    widget.state_path() == "/tmp/relay-widget-test.json")
    finally:
        if old is None:
            del os.environ["RELAY_WIDGET_STATE"]
        else:
            os.environ["RELAY_WIDGET_STATE"] = old
    ok &= check("default path is under ~/.relay",
                widget.state_path().endswith("/.relay/widget.json"))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
