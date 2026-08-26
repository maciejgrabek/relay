"""Workspaces: sessions grouped by the directory they were launched in.

A workspace is WHERE THE SESSION WAS LAUNCHED, full stop (docs/IDEAS.md #14).
Not the git root, not a name the operator assigns, and explicitly NOT a live
cwd that follows whatever `cd` a session runs mid-turn: relay records the
directory once, at the point it first sees the tab, and never chases it after.
The operator's mental model is "the tab I opened in the relay repo", not
"wherever Claude happens to be standing".

Freezing is an ACTIVE choice, not the default - iTerm2 serves a session's path
live and it already follows `cd`, so the capture side has to snapshot it and
then deliberately stop reading it (watcher.SessionInfo.home_dir). This module
is the consumer: it takes whatever key the caller froze and does the ordering.

Two properties make the grouping cheap and safe:

  * The key cannot change in response to session STATE. A row therefore never
    moves because a session started working or went blocked - the one thing
    the stable-list-order rule exists to prevent. It moves only when the
    operator opens or closes a tab, which is a thing they just did.

  * `min_size` is the CALLER's decision, not this module's opinion. The
    control view rails every directory including one-session ones, so the eye
    never has to work out whether a row belongs to the group above it; a
    caller that wants rails only where they carry information passes 2. A key
    that is empty never groups either way - an unreadable directory is not a
    workspace, and pretending otherwise would put unrelated tabs in one box.

Order: a group sits where its FIRST member sat, and members keep their
relative order inside it. So the list is still read top-to-bottom in tab
order; grouping gathers, it does not sort.
"""
import os
from typing import Callable, List, Optional, Sequence, Tuple

# (key or None, [items]) - key None means "render these bare, no rail".
Group = Tuple[Optional[str], list]


def group(items: Sequence, key_of: Callable, min_size: int = 2) -> List[Group]:
    """Gather `items` into railed groups by `key_of`, preserving order.

    A key that is empty/None never groups: an unreadable directory is not a
    workspace, and pretending otherwise would put unrelated tabs in one box.
    """
    order: List[str] = []
    buckets = {}
    solo_at = {}          # position in `order` for ungroupable items
    for idx, it in enumerate(items):
        k = key_of(it) or ""
        if not k:
            marker = f"\0solo{idx}"
            order.append(marker)
            solo_at[marker] = it
            continue
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(it)
    out: List[Group] = []
    for k in order:
        if k in solo_at:
            out.append((None, [solo_at[k]]))
            continue
        members = buckets[k]
        if len(members) < min_size:
            out.extend((None, [m]) for m in members)
        else:
            out.append((k, members))
    return out


def freeze(current: str, persisted: str, live: str) -> str:
    """The workspace key to store for a tab, given what is already frozen
    (`current`), what the DB persisted (`persisted`) and what iTerm2 reports
    right now (`live`).

    Two rules, in this order:

      * A key already frozen is NEVER re-frozen. This is the whole feature -
        if anything here "helpfully" refreshed, rows would shuffle the moment
        a session ran `cd`.
      * The persisted column wins over the live reading. sessions.workdir is
        written once, only into an empty column, so it is the OLDEST reading
        relay has and it survives a relay restart - where the live path would
        otherwise re-freeze on wherever the session had wandered to by then.
    """
    if current:
        return current
    return persisted or live or ""


def short_path(p: str, home: str = "") -> str:
    """`/Users/me/Work/relay` -> `~/Work/relay`.

    Display only. The grouping key stays the full path, because two different
    users' homes must never collapse into one workspace just because they
    render the same.
    """
    if not p:
        return ""
    home = home or os.path.expanduser("~")
    if home and (p == home or p.startswith(home.rstrip("/") + "/")):
        return "~" + p[len(home.rstrip("/")):]
    return p


# Compact forms, for a rule that has to fit inside a table column rather
# than across a whole panel. The glyphs are the ones the rows already use, so
# the short form teaches nothing new: ◉ armed, ‼ wants you, ◈ burning.
_COMPACT = {"plain": "{n}", "armed": "◉{n}", "attention": "‼{n}",
            "burning": "◈{n}"}


def summary(members: Sequence, armed_of: Callable, attention_of: Callable,
            burning_of: Optional[Callable] = None,
            compact: bool = False) -> List[Tuple[str, str]]:
    """The counts that ride a group's top rule, as (text, kind) pairs.

    Kinds - 'plain', 'armed', 'attention', 'burning' - name what the count
    MEANS, not what colour it is; the view maps them to its own palette so a
    theme swap cannot strand a hue in here. Zero counts are omitted rather
    than printed as "0 needs you", which reads as news when it is the absence
    of news.

    `compact` returns the glyph form (`3 · ◉2 · ‼1`) for a rule drawn inside a
    table column, where the spelled-out version is silently truncated by
    whatever width the column happened to get.
    """
    def fmt(n, kind, word):
        return (_COMPACT[kind].format(n=n) if compact else word)

    n = len(members)
    out = [(fmt(n, "plain", f"{n} session{'' if n == 1 else 's'}"), "plain")]
    armed = sum(1 for m in members if armed_of(m))
    if armed:
        out.append((fmt(armed, "armed", f"{armed} armed"), "armed"))
    attn = sum(1 for m in members if attention_of(m))
    if attn:
        out.append((fmt(attn, "attention", f"{attn} needs you"), "attention"))
    if burning_of:
        burning = sum(1 for m in members if burning_of(m))
        if burning:
            out.append((fmt(burning, "burning", f"{burning} burning"),
                        "burning"))
    return out
