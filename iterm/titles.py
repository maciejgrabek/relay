"""Tab-title prefixes - pure render/strip logic (no iterm2 imports).

The watcher writes titles like "✦[BLOCKED] api-server" so the tab bar itself
shows mode + attention state. strip_prefix() is the crash-safety mechanism:
every reader (UNIT column, swarm registry) strips before use, so a prefix
left behind by a crashed run can never pollute names - and render() is always
given an already-stripped bare name, so a double prefix cannot be produced.

Vocabulary is FIXED (it doubles as the strip-parser; configurability would
double the bug surface). Mode glyphs match the TUI's MODE_STYLE.
"""
from __future__ import annotations

import re

MODE_GLYPH = {"safe": "◉", "wild": "▲", "insane": "✦", "shadow": "◌",
              "extreme": "✷"}
MODE_WORD = {"safe": "SAFE", "wild": "WILD", "insane": "INSANE",
             "shadow": "SHADOW", "extreme": "EXTREME"}
# Attention priority: blocked > prompting > stale. One state indicator max.
# stale uses "⧗" - a glyph nobody types in a real tab title, so strip_prefix
# can never eat a user's name.
STATE_GLYPH = {"blocked": "⊘", "prompting": "‼", "stale": "⧗"}
STATE_WORD = {"blocked": "BLOCKED", "prompting": "AWAITING", "stale": "STALE"}

# Strip one leading relay prefix: an optional mode glyph, an optional state
# glyph, then up to two known bracket words, then the separating space.
# Unknown bracket words ([WIP]) don't match, so user titles survive.
# DERIVED from the maps above - a mode added there is automatically
# strippable (the 2026-08-06 ✷ desync stacked one prefix per tick because
# this regex was a hand-written second copy of the vocabulary).
_PREFIX_RE = re.compile(
    "^[" + "".join(MODE_GLYPH.values()) + "]?"
    "[" + "".join(STATE_GLYPH.values()) + "]?"
    "(?:\\[(?:" + "|".join((*MODE_WORD.values(), *STATE_WORD.values()))
    + ")\\]){0,2}"
    " ")


def _attention(state: str, stale: bool) -> str | None:
    """The single state key to show, or None. blocked > prompting > stale."""
    if state == "blocked":
        return "blocked"
    if state == "prompting":
        return "prompting"
    if stale:
        return "stale"
    return None


def render(style: str, mode: str, state: str, stale: bool, bare: str) -> str:
    """Compose the desired on-screen title from an already-STRIPPED name."""
    if style == "off":
        return bare
    att = _attention(state, stale)
    show_mode = mode in MODE_GLYPH
    if not show_mode and att is None:
        return bare                       # manual + nothing to say: untouched

    if style == "glyphs":
        prefix = (MODE_GLYPH.get(mode, "")
                  + (STATE_GLYPH[att] if att else ""))
    elif style == "words":
        prefix = ((f"[{MODE_WORD[mode]}]" if show_mode else "")
                  + (f"[{STATE_WORD[att]}]" if att else ""))
    else:                                 # hybrid: glyph mode, word state
        prefix = (MODE_GLYPH.get(mode, "")
                  + (f"[{STATE_WORD[att]}]" if att else ""))
    return f"{prefix} {bare}" if prefix else bare


def strip_prefix(title: str) -> str:
    """Remove ALL leading relay prefixes; anything else passes through.

    Looping matters: a strip/render vocabulary desync (or repeated crashed
    runs) can leave several stacked prefixes on a real tab; stripping them
    all is what lets a fixed relay self-heal those titles."""
    while title:
        m = _PREFIX_RE.match(title)
        if not (m and m.group(0).strip()):  # require a non-empty actual prefix
            return title
        title = title[m.end():]
    return title
