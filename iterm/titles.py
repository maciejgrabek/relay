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

MODE_GLYPH = {"safe": "◉", "wild": "▲", "insane": "✦"}
MODE_WORD = {"safe": "SAFE", "wild": "WILD", "insane": "INSANE"}
# Attention priority: blocked > prompting > stale. One state indicator max.
STATE_GLYPH = {"blocked": "⊘", "prompting": "‼", "stale": "?"}
STATE_WORD = {"blocked": "BLOCKED", "prompting": "AWAITING", "stale": "STALE"}

# Strip exactly one leading relay prefix: an optional mode glyph, an optional
# state glyph, then up to two known bracket words, then the separating space.
# Unknown bracket words ([WIP]) don't match, so user titles survive.
_PREFIX_RE = re.compile(
    r"^[◉▲✦]?[‼⊘?]?"
    r"(?:\[(?:SAFE|WILD|INSANE|AWAITING|BLOCKED|STALE)\]){0,2}"
    r" ")


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
    """Remove at most one leading relay prefix; anything else passes through."""
    if not title:
        return title
    m = _PREFIX_RE.match(title)
    if m and m.group(0).strip():          # require a non-empty actual prefix
        return title[m.end():]
    return title
