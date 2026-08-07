# DOS-Style TUI Modals - Design Spec

**Date:** 2026-08-07
**Status:** Approved (design)

## Problem

TUI refusals are easy to miss: pressing `E E` on a session that is not
INSANE only writes a line into the scrolling log. The operator who just
pressed a key deserves feedback where their eyes are - a small modal, in
the spirit of old DOS dialogs (double-line box, drop shadow, "press any
key"). First use is the extreme refusal; the component is reusable so
other refusals can convert later if they earn it.

## Design

House pattern, not Textual screens: every existing overlay is a
shown/hidden widget on the single screen, and the 1s refresh loop queries
base-screen widgets directly - a pushed `ModalScreen` would break those
lookups. The modal is a floating `Static` on a dedicated CSS layer.

### 1. Pure renderer

`dos_modal_text(title, lines, width) -> str` in `iterm/app.py`, next to
the other pure panel builders:

- Double-line box: `╔═╗ ║ ╠═╣ ╚═╝`, title row, separator, body lines,
  blank line, centered `[ press any key ]` footer row.
- Drop shadow: `▓` down the right edge (offset one row) and along the
  bottom, offset two columns - the DOS look.
- Width: box inner width clamps to `min(max(len(title), longest line,
  24), width - 4)`; longer body lines are truncated to fit. Lines are
  padded with spaces so the right border aligns (plain text pane;
  single-width glyphs only, same rule as MODE_STYLE).

### 2. Widget + app API

- `compose()` gains `Static(id="modal")` on CSS `layer: modal`, centered
  (`align: center middle` on the layer), `display: none` by default.
  Colors come from theme tokens (border/text amber `$warn` on the CRT
  palette) so theme swaps recolor it like everything else.
- `RelayApp._modal_show(title: str, lines: list[str]) -> None`: render
  via `dos_modal_text` (width from the pane size), set `display: block`,
  set `self._modal_open = True`.
- `RelayApp._modal_close() -> None`: hide, `self._modal_open = False`.
- `_any_overlay_open()` returns True while `_modal_open` - every
  session-mutating binding is already inert behind that guard.
- Key handling: while `_modal_open`, the app-level `on_key` closes the
  modal and stops the event - ANY key dismisses and is swallowed (it
  never also fires its binding). Esc needs no special case beyond this;
  `action_dismiss_view` also closes the modal first if open (parallel to
  the extreme-form check).
- The 1s refresh never touches `#modal`; an open modal simply floats
  over the refreshed list until a key closes it.

### 3. First wiring: the E E refusal

In `action_extreme`, the `mode not in ("insane", "extreme")` branch calls

```python
self._modal_show(
    "EXTREME - NOT AVAILABLE",
    ["This session is not INSANE.",
     "SPACE cycles the arm level to",
     "✦ INSANE, then press E E again."])
```

and keeps the existing log line (log = history, modal = visibility).
The refusal still clears the pending arm state as it does today.

## Testing

`iterm/test_extreme.py` (or a small new suite if cleaner):

- Renderer: box corners/borders present; all rows same display width;
  title and footer rows present; long body line truncated; narrow width
  clamps sanely.
- App (pilot tests, same style as the sid-binding test): `E` on a
  non-insane session sets `_modal_open` and fills `#modal`; any key
  closes it and does NOT trigger that key's action (e.g. `q` closes the
  modal without arming quit); `_any_overlay_open()` is True while open;
  arming from a proper INSANE session never opens it.

## Out of scope

- Converting other refusals (R/W nothing-orphaned, Z multi-project,
  timers own-tab) - surveyed after this ships, converted only if earned.
- Confirm/choice modals or replacing the double-press arming pattern.
- Auto-dismiss timers - the operator just pressed a key; any-key is
  enough.
