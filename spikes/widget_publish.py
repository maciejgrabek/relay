#!/usr/bin/env python3
"""SPIKE: fake publisher for the desktop widget.

Stands in for the real hook in app.py's banner_with_face until iterm/widget.py
exists. Ticks the SAME mascot renderer the TUI uses, so what the widget shows is
what the panel would show - the point is to prove the pipe and the animation,
not to be the shipping publisher.

    python3 spikes/widget_publish.py            # cycle through states
    python3 spikes/widget_publish.py alarmed    # pin one state

See docs/specs/2026-07-28-desktop-widget-design.md.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "iterm"))
import app  # noqa: E402

STATE_PATH = os.path.expanduser(
    os.environ.get("RELAY_WIDGET_STATE", "~/.relay/widget.json"))

# (label, band, awaiting, working, armed, paused) - one per mascot mood, so a
# single run demonstrates every frame the widget has to cope with.
SCENES = [
    ("guarding", "calm",        0, False, 3, False),
    ("working",  "calm",        0, True,  3, False),
    ("alarmed",  "calm",        2, False, 3, False),
    ("critical", "☢ CRITICAL", 0, False, 3, False),
    ("paused",   "calm",        0, False, 3, True),
    ("idle",     "calm",        0, False, 0, False),
]


# The bubble's text row is rendered as "◃ <phrase> │" by app._speech_bubble.
# Compact mode has no creature to read it from, so the phrase is published as
# its own field rather than left buried in the art.
_SPEECH_RE = re.compile(r"◃\s*(.*?)\s*│")


def speech(art) -> str:
    for line in art:
        m = _SPEECH_RE.search(line)
        if m:
            return m.group(1)
    return ""


def frame(tick, band, awaiting, working, armed, paused):
    face = app.mascot_face_big(tick, band, awaiting=awaiting, working=working,
                               armed=armed, approvals=7, paused=paused)
    state = app.effective_mascot_state(band, awaiting=awaiting,
                                       working=working, armed=armed,
                                       paused=paused)
    # Frames are built bracket-free for Textual, but strip defensively so no
    # markup can ever reach the widget's <pre>.
    art = [re.sub(r"\[/?[^\]]*\]", "", l).rstrip() for l in face]
    return {
        "ts": time.time(),
        "state": state,
        "color": app._MASCOT_COLOR[state],
        "art": art,
        "phrase": speech(art),
        "armed": armed,
        "awaiting": awaiting,
        "working": working,
        "paused": paused,
        "band": band,
        "sessions": 7,
    }


def publish(payload):
    """Atomic tmp + replace, so the widget can never read a torn file."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, STATE_PATH)


def main():
    pin = sys.argv[1] if len(sys.argv) > 1 else None
    scenes = [s for s in SCENES if s[0] == pin] or SCENES
    if pin and not any(s[0] == pin for s in SCENES):
        print(f"unknown state {pin!r}; try: "
              + ", ".join(s[0] for s in SCENES), file=sys.stderr)
        return 1
    print(f"publishing to {STATE_PATH} (ctrl-c to stop)")
    tick, i, held = 0, 0, 0
    try:
        while True:
            _, band, awaiting, working, armed, paused = scenes[i % len(scenes)]
            publish(frame(tick, band, awaiting, working, armed, paused))
            time.sleep(0.5)          # matches the TUI's reactor tick
            tick += 1
            held += 1
            if held >= 16:           # ~8s per scene
                held, i = 0, i + 1
    except KeyboardInterrupt:
        print("\nstopped (widget goes offline in 5s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
