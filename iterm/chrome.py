"""Panel chrome: the boxes and rails every relay view is drawn in.

One grammar, four rules (docs/IDEAS.md #17):

  1. The title lives in the border. A panel names itself on its own top edge -
     no header row, no label line, no separator underneath it.
  2. The edge is the grouping. Rows that belong together share a rail.
  3. The summary rides the border too, on the right of the top edge, so a
     panel can be read without reading its rows.
  4. Borders carry names and counts, NEVER keys. The key bar keeps every key;
     a key in a border as well would be the same key twice on one screen.

Two shapes, and the difference between them is not decoration:

  panel_*  is CLOSED - `┌─title ── right ┐`, side bars, `└────┘`. Use it where
           the width is known and fixed for every line the caller emits (a
           Static the caller renders in full). A closed box says "this is a
           container".

  rail_*   is OPEN on the right - a HEAVY left bar, light top and bottom
           rules, and no right-hand edge at all. Use it for a group INSIDE a
           panel. A rail says "these rows belong together", which is the truer
           claim for a workspace: the sessions stay one flat list and the rail
           is an annotation on it.

The open shape is not a compromise, it is the cheaper contract. A right-hand
edge has to land on the same column on every row, so every column feeding it
must be pinned; a rail is one glyph at the start of a row and the rules can
end wherever they end. Where a right edge is free (a container that knows its
own width) it is drawn; where it would cost pinned columns it is dropped.

The joints are `┎` (U+250E) and `┖` (U+2516) - "heavy down/up and light
right". They exist for exactly this, and using them is why the weight change
reads as intentional instead of as two box styles colliding.

Every function returns MARKUP and is pure. Callers pass PLAIN titles (this
module colors them); `side()` accepts content that already carries markup and
measures it correctly, because the alternative - measuring with len() - counts
tag characters as columns and pushes the right edge off by however much color
the caller used.
"""
from typing import List, Optional

try:                                  # rich is always present (Textual needs it)
    from rich.markup import escape as _escape
except Exception:                     # pragma: no cover - keeps the module pure
    def _escape(t):
        return t

H = "─"          # ─ light horizontal
V = "│"          # │ light vertical
TL, TR = "┌", "┐"      # ┌ ┐
BL, BR = "└", "┘"      # └ ┘
RAIL = "┃"       # ┃ heavy vertical
RAIL_TOP = "┎"   # ┎ heavy down + light right
RAIL_BOT = "┖"   # ┖ heavy up + light right

MIN_WIDTH = 12        # below this a box is noise; callers get plain lines


def cells(s: str) -> int:
    """Visible width of a MARKUP string in terminal cells.

    len() is wrong twice over here: it counts `[bold red]` as ten columns, and
    it counts a CJK session title as half the space it takes. Rich already
    knows both answers, so ask it rather than keeping a second opinion.
    Unparseable markup falls back to the raw cell count - a slightly wrong
    width beats an exception thrown from a repaint.
    """
    try:
        from rich.text import Text
        return Text.from_markup(s).cell_len
    except Exception:
        try:
            from rich.cells import cell_len
            return cell_len(s)
        except Exception:
            return len(s)


def _clip(s: str, width: int, marker: str = "…") -> str:
    """Clamp PLAIN text to `width` cells, marking a cut. Never call this on
    markup: slicing mid-tag stops it being a tag and corrupts every line after
    it, which is worse than the overflow it was meant to prevent."""
    if width <= 0:
        return ""
    if cells(s) <= width:
        return s
    out = ""
    for ch in s:
        if cells(out + ch) > width - cells(marker):
            break
        out += ch
    return out + marker


def _wrap(s: str, color: str) -> str:
    return f"[{color}]{s}[/]" if color and s else s


def _edge(corner_l: str, corner_r: Optional[str], title: str, right: str,
          width: int, color: str, title_color: str, right_color: str,
          esc: bool = True) -> str:
    """Shared top-edge builder. `corner_r` None means the edge is OPEN - no
    right corner, no reserved column for it.

    Titles are PLAIN text and are escaped here, after clipping: a workdir or a
    session name can contain `[`, and escaping before the clip risks cutting a
    `\\[` in half, which stops it being an escape and corrupts the line.
    """
    closed = corner_r is not None
    # Reserved: corner + one rule cell on the left, a space after the title,
    # and (closed only) the right corner.
    fixed = 2 + 1 + (1 if closed else 0)
    room = max(0, width - fixed)
    title = _clip(title, room)
    room -= cells(title)
    # The right label is expendable before the title is: the title says which
    # panel this is, the summary is a nicety that can also be read elsewhere.
    tail = ""
    if right:
        need = cells(right) + 4          # "── " + label + " "
        if need <= room:
            tail = right
            room -= need
    if esc:
        title, tail = _escape(title), _escape(tail)
    line = _wrap(corner_l + H, color) + _wrap(title, title_color) + " "
    line += _wrap(H * room, color)
    if tail:
        line += _wrap(H + H + " ", color) + _wrap(tail, right_color) + " "
    if closed:
        line += _wrap(corner_r, color)
    return line


def panel_top(title: str, right: str = "", width: int = 80, color: str = "",
              title_color: str = "", right_color: str = "",
              esc: bool = True) -> str:
    """`┌─title ────────── ── right ┐`, exactly `width` cells.

    `esc=False` for a pane rendered with markup OFF (the audit view lives in
    the markup-free preview pane): escaping there would print the backslashes
    it adds instead of hiding them.
    """
    return _edge(TL, TR, title, right, width, color,
                 title_color or color, right_color or color, esc)


def panel_bottom(width: int = 80, color: str = "") -> str:
    """`└──────┘`, exactly `width` cells."""
    return _wrap(BL + H * max(0, width - 2) + BR, color)


def side(content: str, width: int = 80, color: str = "") -> str:
    """`│ content ... │` - content may carry markup and is padded, never cut,
    on the assumption the caller already fitted it. Over-long content pushes
    the right bar out by exactly its overflow instead of corrupting markup."""
    inner = max(0, width - 2)
    pad = " " * max(0, inner - cells(content))
    return _wrap(V, color) + content + pad + _wrap(V, color)


def panel(lines: List[str], title: str, right: str = "", width: int = 80,
          color: str = "", title_color: str = "", right_color: str = "",
          pad: int = 1) -> List[str]:
    """A closed panel around `lines`. Below MIN_WIDTH the chrome is dropped
    and the lines are returned bare: a box narrower than its own title is not
    a frame, it is noise with the content squeezed out of it."""
    if width < MIN_WIDTH:
        return list(lines)
    lead = " " * pad
    return ([panel_top(title, right, width, color, title_color, right_color)]
            + [side(lead + ln, width, color) for ln in lines]
            + [panel_bottom(width, color)])


def rule(title: str = "", right: str = "", width: int = 80, color: str = "",
         title_color: str = "", right_color: str = "", esc: bool = True) -> str:
    """`── title ──────────── ── right` - a titled separator for a SECTION.

    Not a group and not a panel: a section is a run of lines that belongs
    under one heading but does not bind its rows to each other, so it gets a
    rule rather than a rail. Same edge grammar, one weight down.
    """
    # One leading space the panel edges do not take: `┌─sessions` reads as a
    # corner biting into its title, `── interactions` reads as a rule the
    # title sits on, and they are different objects.
    return _edge(H, None, " " + title if title else "", right, width, color,
                 title_color or color, right_color or color, esc)


def rail_top(title: str, right: str = "", width: int = 80, color: str = "",
             title_color: str = "", right_color: str = "") -> str:
    """`┎─title ──────── ── right` - open on the right, so the caller owes it
    no column agreement at all."""
    return _edge(RAIL_TOP, None, title, right, width, color,
                 title_color or color, right_color or color)


def rail_row(content: str, color: str = "") -> str:
    """`┃content` - one glyph, prepended. That is the whole cost of a group."""
    return _wrap(RAIL, color) + content


def rail_bottom(width: int = 80, color: str = "") -> str:
    """`┖───────` - closes the rail without closing the box."""
    return _wrap(RAIL_BOT + H * max(0, width - 1), color)


def rail_group(lines: List[str], title: str, right: str = "", width: int = 80,
               color: str = "", title_color: str = "",
               right_color: str = "") -> List[str]:
    """A railed group: top rule, `┃`-prefixed rows, bottom rule."""
    if width < MIN_WIDTH:
        return list(lines)
    return ([rail_top(title, right, width, color, title_color, right_color)]
            + [rail_row(ln, color) for ln in lines]
            + [rail_bottom(width, color)])
