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
    reveal, color = strike_state(tick)
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
    body.append(_center(len(SUBTITLE), cols) + _tag(pal, "dim", SUBTITLE))
    body.append("")

    rule_w = min(max(cols - 6, 10), label_w + 44)
    body.append(_center(rule_w, cols) + _tag(pal, "dimmer", "─" * rule_w))
    body.append("")

    left = _center(label_w + 40, cols)
    for st in steps:                      # not `step` - that is the factory
        label = _tag(pal, "dim", f"{st.label:<{label_w}} : ")
        if st.value is not None:
            value = _tag(pal, st.color, _esc(st.value))
        else:
            value = _tag(pal, "cyan", SPIN[tick % len(SPIN)])
        body.append(left + label + value)

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


# name -> renderer. THIS is the plug point: add an entry, add a function, done.
_STYLES: Dict[str, Callable[..., str]] = {
    "bios": _bios,
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
