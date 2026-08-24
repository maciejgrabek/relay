"""The boot screen - the full-screen sequence relay plays while it wakes up.

Relay's startup is not instant: the iTerm2 handshake, the first screen sweep of
every session, the audit prune and the classifier load all cost real time. That
time is going to pass whether or not anything is on screen, so this fills it
with something worth looking at AND worth reading - every line below the logo
is a subsystem reporting its real state, so a boot that stalls tells you WHICH
part stalled instead of leaving you at a blank panel.

PLUGGABLE BY NAME, on purpose. A boot style is one entry in `_STYLES`: a name
mapped to a renderer with the signature

    renderer(steps, *, tick, cols, rows, pal) -> str

Adding a second style is a dict entry plus a function in this file. Nothing in
app.py, config.py or settings.py learns the new name beyond `BOOT_STYLES`, and
removing a style removes exactly itself. app.py's side is a guarded import and
one overlay, so deleting this module entirely leaves relay working - the boot
screen is an accessory to startup, never a step in it.

Everything here is PURE. No Textual, no iTerm2, no I/O, no clock - the caller
passes the tick and the palette, which is what makes the whole sequence
testable frame by frame at any terminal size. The palette arrives as a plain
dict of colour-name -> hex so this module never imports app.py (which imports
Textual); that one-way dependency is what keeps it a plugin rather than a limb.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# The block logo. Deliberately duplicated from app.py rather than imported:
# importing app.py from here would pull in Textual and invert the dependency
# that makes this module droppable. Six lines of ASCII is the cheaper copy.
LOGO = r"""
 ██████╗ ███████╗██╗      █████╗ ██╗   ██╗
 ██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝
 ██████╔╝█████╗  ██║     ███████║ ╚████╔╝
 ██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝
 ██║  ██║███████╗███████╗██║  ██║   ██║
 ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝""".strip("\n").split("\n")

LOGO_W = max(len(line) for line in LOGO)

# The logo strikes on left to right like a phosphor tube, brightening as it
# goes. Expressed as palette NAMES rather than scaled RGB so an amber or ice
# theme strikes in its own colours without this module knowing they exist.
STRIKE_TICKS = 12
_STRIKE_COLORS = ("dimmer", "dim", "accent", "bright")

SPIN = "⣾⣽⣻⢿⡿⣟⣯⣷"

SUBTITLE = "S E S S I O N   C O N T R O L"
WELCOME = "WELCOME, OPERATOR"
# The logo's stand-in wherever the block letterform will not fit.
WORDMARK = "r e l a y"


@dataclass
class Step:
    """One POST line.

    `value` and `done` are deliberately SEPARATE. Having something to display
    is not the same as having finished: a memory count ticking upward shows a
    new value every frame and is not done until it lands. Deriving one from
    the other froze the counter on its first frame, because the first digit
    written looked like a result.

    `color` is a palette key, so a step can report in `warn` without this
    module deciding what warning looks like.
    """
    label: str
    value: Optional[str] = None
    color: str = "accent"
    done: bool = False

    def report(self, value: str, color: str = "accent") -> "Step":
        """The subsystem has finished. The only way a step becomes done."""
        self.value = value
        self.color = color
        self.done = True
        return self

    def progress(self, value: str, color: str = "bright") -> "Step":
        """Something to show while still working - a count, a partial result.
        Does NOT complete the step."""
        self.value = value
        self.color = color
        return self


def step(label: str, value: Optional[str] = None,
         color: str = "accent") -> Step:
    """Build a Step. Passing a value here means the subsystem had already
    reported by the time the screen was built (config, the audit prune), so
    such a step starts done."""
    return Step(label, value, color, done=value is not None)


def _esc(text: str) -> str:
    """Neutralize Rich markup in a value. Subsystem values carry paths and
    commands; a stray '[' must render, not open a tag."""
    return text.replace("[", r"\[")


def _tag(pal: Dict[str, str], key: str, text: str) -> str:
    return f"[{pal.get(key, pal.get('dim', 'white'))}]{text}[/]"


def _center(text_len: int, cols: int) -> str:
    return " " * max(0, (cols - text_len) // 2)


def strike_state(tick: int) -> tuple:
    """(reveal fraction, palette colour name) for the logo at `tick`. Pure and
    separately testable because the strike is the one part driven by the clock
    rather than by what the subsystems reported."""
    if tick >= STRIKE_TICKS:
        return (1.0, "bright")
    reveal = max(0.0, tick / STRIKE_TICKS)
    idx = min(len(_STRIKE_COLORS) - 1, int(tick * len(_STRIKE_COLORS) / STRIKE_TICKS))
    return (reveal, _STRIKE_COLORS[idx])


def _logo_lines(tick: int, cols: int, pal: Dict[str, str]) -> List[str]:
    """The struck logo, or the wordmark when the logo cannot fit.

    A 42-column block letterform in a 30-column panel does not degrade, it
    wraps into rubble - and the panel is resizable, so that is a real width.
    The fallback keeps the strike (same colour ramp, same reveal), so a narrow
    terminal gets a smaller version of the same animation rather than a
    different screen."""
    reveal, color = strike_state(tick)
    if cols < LOGO_W + 2:
        cut = max(0, int(len(WORDMARK) * reveal))
        return [_center(len(WORDMARK), cols)
                + _tag(pal, color, WORDMARK[:cut])
                + _tag(pal, "dimmer", WORDMARK[cut:])]
    cut = int(LOGO_W * reveal)
    pad = _center(LOGO_W, cols)
    lines = []
    for raw in LOGO:
        lit, rest = raw[:cut], raw[cut:]
        line = pad + _tag(pal, color, lit)
        if rest.strip():
            line += _tag(pal, "dimmer", rest)
        lines.append(line)
    return lines


def _bios(steps: List[Step], *, tick: int, cols: int, rows: int,
          pal: Dict[str, str]) -> str:
    """The shipped style: centred logo, a POST block of real subsystem state,
    and a termlink sign-off once everything has reported."""
    label_w = max([len(s.label) for s in steps], default=0)
    body: List[str] = []

    body += _logo_lines(tick, cols, pal)
    if len(SUBTITLE) + 2 <= cols:
        body.append(_center(len(SUBTITLE), cols) + _tag(pal, "dim", SUBTITLE))
    body.append("")

    rule_w = min(max(cols - 6, 10), label_w + 44)
    body.append(_center(rule_w, cols) + _tag(pal, "dimmer", "─" * rule_w))
    body.append("")

    left = _center(label_w + 40, cols)
    room = max(0, cols - len(left) - label_w - 3)
    for st in steps:                      # not `step` - that is the factory
        name = _clip(st.label, max(1, cols - 3))
        label = _tag(pal, "dim", f"{name:<{label_w}} : ")
        body.append(left + label + _value_or_spin(st, tick, pal, room))

    body.append("")
    if steps and all(s.done for s in steps):
        cursor = "█" if tick % 8 < 5 else " "
        body.append(_center(len(WELCOME) + 2, cols)
                    + _tag(pal, "hot", WELCOME + " " + cursor))
    else:
        body.append("")

    # Centre the whole block vertically so it reads as a splash, not a log.
    top_pad = max(0, (rows - len(body)) // 2)
    return "\n" * top_pad + "\n".join(body)


# --- shared bits for the styles below ------------------------------------

def _value_or_spin(st: Step, tick: int, pal: Dict[str, str],
                   room: Optional[int] = None) -> str:
    """A step's reported value, or the spinner while it is still working.
    Every style asks the same question of a Step and must answer it the same
    way, or two styles would disagree about whether a subsystem had reported.

    `room` clips the value first: a path is the longest thing a subsystem ever
    reports and a 40-column panel is a real width."""
    if st.value is not None:
        value = st.value if room is None else _clip(st.value, room)
        return _tag(pal, st.color, _esc(value))
    return _tag(pal, "cyan", SPIN[tick % len(SPIN)])


def _clip(text: str, width: int) -> str:
    """Plain text cut to width. Callers clip BEFORE tagging, so a cut can never
    land inside markup and leave an unclosed tag on screen."""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


# THE CONTRACT EVERY STYLE KEEPS. A boot screen earns its place by turning a
# stalled start into a diagnosis, so whatever else a style does, it must:
#   1. name the subsystem it is still waiting on,
#   2. show a value the moment its step reports one,
#   3. hold the sign-off until every step is done.
# Anything that only entertains is a splash, and a splash is what `[boot]
# enabled = false` is for.

BAR_ON, BAR_OFF = "▰", "▱"


def _minimal(steps: List[Step], *, tick: int, cols: int, rows: int,
             pal: Dict[str, str]) -> str:
    """The quiet style: a wordmark, a progress bar, and the name of whatever
    relay is still waiting on.

    This exists so that "the splash is too much" has an answer other than
    turning the boot screen off - which trades away the noise and the stall
    diagnosis with it. One line still says Sessions when Sessions is what has
    not come back."""
    total = len(steps)
    done = sum(1 for s in steps if s.done)
    _, strike = strike_state(tick)

    body = [_center(len(WORDMARK), cols) + _tag(pal, strike, WORDMARK), ""]

    bar_w = max(1, min(total * 3, max(4, cols - 20))) if total else 1
    filled = (done * bar_w // total) if total else 0
    pending = next((s for s in steps if not s.done), None)
    label = _clip(pending.label if pending else "", max(0, cols - bar_w - 4))

    width = bar_w + (len(label) + 1 if label else 0)
    line = (_center(width, cols)
            + _tag(pal, "accent", BAR_ON * filled)
            + _tag(pal, "dimmer", BAR_OFF * (bar_w - filled)))
    if label:
        line += " " + _tag(pal, "dim", label)
    body.append(line)

    # The most recent report, one line, dim. Quiet is the point of this style
    # but SILENT is not: a subsystem that came back with something worth
    # printing (a warning, a path, a count still climbing) gets its moment
    # here, and the operator watching a slow boot sees progress rather than a
    # bar that could be stuck. `value`, not `done`, so a counter shows.
    latest = next((s for s in reversed(steps) if s.value is not None), None)
    if latest is not None:
        text = _clip(f"{latest.label} · {latest.value}", max(0, cols - 4))
        body += ["", _center(len(text), cols)
                 + _tag(pal, "dimmer", _esc(text))]

    if total and done == total:
        cursor = "█" if tick % 8 < 5 else " "
        body += ["", _center(len(WELCOME) + 2, cols)
                 + _tag(pal, "hot", WELCOME + " " + cursor)]

    top_pad = max(0, (rows - len(body)) // 2)
    return "\n" * top_pad + "\n".join(body)


# Eight cells wide, all of them, so the marks line up in one column and the
# eye reads the exceptions without reading the words.
_MARKS = {"ok": "[  OK  ]", "warn": "[ WARN ]", "fail": "[ FAIL ]"}


def _console(steps: List[Step], *, tick: int, cols: int, rows: int,
             pal: Dict[str, str]) -> str:
    """The boot-log style: top-anchored, left-aligned, one bracketed verdict
    per subsystem.

    This is the style that makes a problem LOUD. A step reporting in `warn` or
    `danger` gets [ WARN ] / [ FAIL ] in the same column every other line says
    [  OK  ] - a shape people have been reading on boot logs for thirty years,
    and one the centred styles cannot make as scannable."""
    left = "  "
    head = _clip(f"relay · {SUBTITLE}", cols - len(left))
    body = [left + _tag(pal, "dim", head),
            left + _tag(pal, "dimmer", "─" * len(head)),
            ""]

    room = max(0, cols - len(left) - 9)          # mark + one space
    label_w = min(max([len(s.label) for s in steps], default=0), room)
    for st in steps:
        # The marks are ESCAPED like any value: '[  OK  ]' is markup to
        # Textual, and an unescaped one either vanishes from the screen or
        # raises on an unknown style name.
        if not st.done:
            mark = _tag(pal, "cyan", _esc(f"[  {SPIN[tick % len(SPIN)]}   ]"))
        elif st.color == "danger":
            mark = _tag(pal, "danger", _esc(_MARKS["fail"]))
        elif st.color == "warn":
            mark = _tag(pal, "warn", _esc(_MARKS["warn"]))
        else:
            mark = _tag(pal, "accent", _esc(_MARKS["ok"]))
        label = _clip(st.label, room)
        line = left + mark + " "
        value = (_clip(st.value, max(0, room - label_w - 2))
                 if st.value is not None else "")
        if value:
            line += (_tag(pal, "dim", label.ljust(label_w)) + "  "
                     + _tag(pal, st.color, _esc(value)))
        else:
            # No value to align against yet, so no padding: a row of trailing
            # spaces is invisible on screen and noise in every test that reads
            # the frame back.
            line += _tag(pal, "dim", label)
        body.append(line)

    if steps and all(s.done for s in steps):
        cursor = "█" if tick % 8 < 5 else " "
        sign = _clip(WELCOME + " " + cursor, cols - len(left))
        body += ["", left + _tag(pal, "hot", sign)]
    return "\n".join(body)


def _crt(steps: List[Step], *, tick: int, cols: int, rows: int,
         pal: Dict[str, str]) -> str:
    """The phosphor style: the POST block under a scanline that sweeps down
    the frame, with dot leaders instead of a colon.

    Rows are built as (text, colour) segments and coloured LAST, which is what
    lets one row be the scanline without any other row knowing it exists. The
    sweep changes colour only - the text of a frame is the text of the same
    frame in `bios`, so a stalled boot reads identically."""
    lines: List[List[tuple]] = []

    reveal, strike = strike_state(tick)
    if cols < LOGO_W + 2:                 # same fallback as _logo_lines
        cut = max(0, int(len(WORDMARK) * reveal))
        lines.append([(_center(len(WORDMARK), cols) + WORDMARK[:cut], strike),
                      (WORDMARK[cut:], "dimmer")])
    else:
        cut = int(LOGO_W * reveal)
        pad = _center(LOGO_W, cols)
        for raw in LOGO:
            lines.append([(pad + raw[:cut], strike), (raw[cut:], "dimmer")])
    if len(SUBTITLE) + 2 <= cols:
        lines.append([(_center(len(SUBTITLE), cols) + SUBTITLE, "dim")])
    lines.append([("", "dim")])

    label_w = max([len(s.label) for s in steps], default=0)
    val_w = max([len(s.value or SPIN[0]) for s in steps], default=0)
    inner = min(max(cols - 6, 10), label_w + val_w + 6)
    left = _center(inner, cols)
    for st in steps:
        # The leader count is measured on the RAW value: `_esc` adds a
        # backslash that Textual never draws, so escaping first would push
        # every dotted line one cell short of its neighbours.
        raw_v = st.value if st.value is not None else SPIN[tick % len(SPIN)]
        raw_v = _clip(raw_v, max(1, inner - len(st.label) - 3))
        dots = max(1, inner - len(st.label) - len(raw_v) - 2)
        lines.append([(left + st.label + " ", "dim"),
                      ("·" * dots, "dimmer"),
                      (" " + _esc(raw_v),
                       st.color if st.value is not None else "cyan")])

    if steps and all(s.done for s in steps):
        cursor = "█" if tick % 8 < 5 else " "
        lines.append([("", "dim")])
        lines.append([(_center(len(WELCOME) + 2, cols) + WELCOME + " " + cursor,
                       "hot")])

    # +4 so the sweep spends a beat off the bottom edge instead of wrapping
    # straight back to the logo, which reads as a stutter rather than a scan.
    scan = tick % (len(lines) + 4)
    out = [
        "".join(_tag(pal, "bright" if i == scan else c, t) for t, c in segs)
        for i, segs in enumerate(lines)
    ]
    top_pad = max(0, (rows - len(out)) // 2)
    return "\n" * top_pad + "\n".join(out)


# name -> renderer. THIS is the plug point: add an entry, add a function, done.
_STYLES: Dict[str, Callable[..., str]] = {
    "bios": _bios,          # the full-screen POST (default)
    "console": _console,    # a top-anchored boot log, verdicts in a column
    "crt": _crt,            # the POST under a phosphor scanline
    "minimal": _minimal,    # a wordmark, a bar, and what it is waiting on
}

BOOT_STYLES = tuple(_STYLES)
DEFAULT_STYLE = "bios"


def render(steps: List[Step], *, tick: int, cols: int, rows: int,
           pal: Dict[str, str], style: str = DEFAULT_STYLE) -> str:
    """One full-screen frame. An unknown style falls back to the default rather
    than raising: a boot screen must never be the reason relay fails to start."""
    renderer = _STYLES.get(style, _STYLES[DEFAULT_STYLE])
    return renderer(steps, tick=tick, cols=max(20, cols),
                    rows=max(10, rows), pal=pal)


def finished(steps: List[Step]) -> bool:
    """True once every step has reported. The caller decides what to do about
    it - this module never dismisses itself."""
    return bool(steps) and all(s.done for s in steps)
