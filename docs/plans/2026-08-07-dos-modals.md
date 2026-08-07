# DOS-Style TUI Modals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/specs/2026-08-07-dos-modals-design.md` - read it first.

**Goal:** A reusable DOS-style modal (double-line box, drop shadow, press
any key) floating over the relay TUI, first wired to the `E E`
requires-INSANE refusal.

**Architecture:** House pattern, not Textual screens: a pure text renderer
(`dos_modal_text`) draws the DOS chrome, a `Static(id="modal")` on a
dedicated CSS layer floats it centered over the UI, and
`_modal_show`/`_modal_close` plus a `_modal_open` flag (folded into
`_any_overlay_open()`) make it the reusable API. Any key closes it and is
swallowed.

**Tech Stack:** Python 3, Textual 8.2.7 (single-screen app, CSS layers),
plain `__main__` test suites run via `./test/run.sh`.

## Global Constraints

- NEVER use the em-dash character (U+2014) anywhere - plain ASCII `-` only.
- Commit messages in repo style; NO `Co-Authored-By` line.
- Box glyphs are single-width: `╔ ═ ╗ ║ ╠ ╣ ╚ ╝ ▓` only; no emoji.
- The modal is display-only chrome: it must not change what any watcher
  logic does - the E E refusal still logs its line and still clears the
  pending arm state exactly as today.
- Tests go in `iterm/test_extreme.py` (renderer + app behavior live with
  the feature that first uses them); existing tests must not regress.
- Tests are hermetic: `RELAY_CONFIG` already points at a nonexistent path
  in that suite - keep it that way.
- Run a suite with `python3 iterm/test_extreme.py`; finish with
  `./test/run.sh` (must print `ALL SUITES PASSED`).

---

### Task 1: `dos_modal_text` - the pure renderer

**Files:**
- Modify: `iterm/app.py` (add the function next to the other pure panel
  builders, e.g. directly above `getting_started_panel`, ~line 600)
- Test: `iterm/test_extreme.py`

**Interfaces:**
- Produces: `dos_modal_text(title: str, lines: list[str], width: int) ->
  str` - the full modal text block, shadow included. Task 2 renders this
  into the `#modal` Static.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_extreme.py` (and call `test_dos_modal_text()` from the
`__main__` runner, before `test_statusbar_label()`):

```python
def test_dos_modal_text():
    import app as appmod
    t = appmod.dos_modal_text("EXTREME - NOT AVAILABLE",
                              ["This session is not INSANE.",
                               "SPACE cycles the arm level."], 80)
    rows = t.splitlines()
    chk("top border is double-line", rows[0].startswith("╔")
        and rows[0].rstrip().endswith("╗") and "═" in rows[0])
    chk("title row present", "EXTREME - NOT AVAILABLE" in rows[1])
    chk("separator row present", rows[2].startswith("╠")
        and rows[2].rstrip().endswith("╣"))
    chk("body line present", any("not INSANE" in r for r in rows))
    chk("footer prompt present", any("press any key" in r for r in rows))
    chk("bottom border + shadow", any(r.startswith("╚") for r in rows)
        and rows[-1].strip().strip("▓") == "")
    box = [r for r in rows if r and r[0] in "╔║╠╚"]
    chk("all box rows same width",
        len({len(r.rstrip("▓")) for r in box}) == 1)
    t2 = appmod.dos_modal_text("T", ["x" * 200], 40)
    chk("long body line truncated to width",
        all(len(r) <= 40 for r in t2.splitlines()))
    t3 = appmod.dos_modal_text("T", ["hi"], 10)
    chk("narrow width clamps sanely (min inner width holds)",
        "press any key" in t3 or "hi" in t3)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_extreme.py`
Expected: `AttributeError: module 'app' has no attribute 'dos_modal_text'`

- [ ] **Step 3: Implement**

In `iterm/app.py`, above `getting_started_panel`:

```python
def dos_modal_text(title: str, lines: list, width: int) -> str:
    """A DOS-style dialog as plain text: double-line box, title row,
    separator, body, centered '[ press any key ]' footer, and a ▓ drop
    shadow (right edge offset one row, bottom row offset two columns).
    Pure and width-clamped; single-width glyphs only (the pane renders
    literally)."""
    inner = max(len(title), max((len(l) for l in lines), default=0),
                len("[ press any key ]"), 24)
    inner = min(inner, max(24, width - 6))
    def clip(s):
        return s[:inner]
    body = [clip(l) for l in lines]
    foot = "[ press any key ]".center(inner)
    top = "╔" + "═" * (inner + 2) + "╗"
    sep = "╠" + "═" * (inner + 2) + "╣"
    bot = "╚" + "═" * (inner + 2) + "╝"
    def row(s):
        return f"║ {s:<{inner}} ║"
    out = [top, row(clip(title)), sep]
    out += [row(l) for l in body]
    out += [row(""), row(foot), bot]
    # Drop shadow: ▓ down the right edge from the second row, and a
    # bottom row indented two columns - the classic DOS offset.
    shaded = [out[0]] + [r + "▓" for r in out[1:]]
    shaded.append("  " + "▓" * (inner + 3))
    return "\n".join(shaded)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 iterm/test_extreme.py`
Expected: all PASS, `ALL PASSED`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_extreme.py
git commit -m "feat(tui): dos_modal_text - the DOS dialog renderer"
```

---

### Task 2: Floating `#modal` layer, app API, E E wiring

**Files:**
- Modify: `iterm/app.py` (CSS ~line 845-879, `compose` ~line 978-992,
  `__init__` flags ~line 945, `_any_overlay_open` ~line 955, `on_key`
  ~line 1750, `action_dismiss_view`, `action_extreme` refusal branch,
  new `_modal_show`/`_modal_close` next to the extreme form helpers)
- Test: `iterm/test_extreme.py`

**Interfaces:**
- Consumes: `dos_modal_text(title, lines, width)` from Task 1.
- Produces: `RelayApp._modal_show(title: str, lines: list) -> None`,
  `RelayApp._modal_close() -> None`, `RelayApp._modal_open: bool`.
  Future refusal conversions call `_modal_show` only.

- [ ] **Step 1: Write the failing tests**

Add to `iterm/test_extreme.py` (pilot-style, mirroring
`test_extreme_arm_sid_binding`'s `_TestApp` scaffolding - reuse that
scaffold; call `test_modal()` from `__main__`):

The suite already has `_ExtremeStubWatcher` and a `_TestApp` pattern in
`test_extreme_arm_sid_binding` (~line 314) - reuse both. If `_TestApp`
is local to that function, lift it to module level once so both tests
share it (do not duplicate the class).

```python
def test_modal():
    """The E E refusal floats a DOS modal; any key closes it and is
    swallowed (no binding fires); the overlay guard holds while open."""
    import asyncio as _aio
    import app as appmod
    from watcher import SessionInfo

    class _ModalApp(appmod.RelayApp):
        def __init__(self, sessions, **k):
            super().__init__(**k)
            self._stub = sessions

        async def _connect(self):
            self.watcher = _ExtremeStubWatcher(self._stub)
            self._running_cfg = self.watcher.cfg
            self._working_cfg = self.watcher.cfg

    sessions = {
        "m1": SessionInfo("m1", title="one", window_idx=0, tab_idx=0,
                          mode="safe"),
        "m2": SessionInfo("m2", title="two", window_idx=0, tab_idx=1,
                          mode="insane"),
    }

    async def run():
        a = _ModalApp(sessions, dry_run=True)
        async with a.run_test() as pilot:
            await pilot.pause()
            a._refresh()
            await pilot.pause()
            t = a.query_one(appmod.DataTable)

            t.move_cursor(row=a._row_sids.index("m1"))
            await pilot.pause()
            a.action_extreme()
            chk("E on a safe session opens the modal", a._modal_open)
            m = a.query_one("#modal")
            chk("#modal is displayed and filled",
                str(m.styles.display) != "none"
                and "NOT AVAILABLE" in str(m.render()))
            chk("overlay guard holds while modal open",
                a._any_overlay_open())

            a.action_extreme()
            chk("actions are inert behind the modal (no arm)",
                a._extreme_armed is None)

            await pilot.press("q")
            chk("any key closes the modal", not a._modal_open)
            chk("the key is swallowed - q did not arm quit",
                not a._quit_armed)
            chk("app still running after swallowed q", a.is_running)

            t.move_cursor(row=a._row_sids.index("m2"))
            await pilot.pause()
            a.action_extreme()
            chk("INSANE session arms without a modal",
                a._extreme_armed == "m2" and not a._modal_open)

    _aio.run(run())
```

Note the refusal's arm-state rule is already covered by
`test_extreme_arm_sid_binding` - do not re-test it here.

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_extreme.py`
Expected: FAIL (no `_modal_open` attribute / modal never opens).

- [ ] **Step 3: Implement**

All in `iterm/app.py`:

1. CSS: extend the `Screen` rule and add the modal rule:

```css
    Screen { background: $bg; color: $bright; layers: base modal; align: center middle; }
    #modal {
        layer: modal; display: none;
        width: auto; height: auto;
        background: $bg_deep; color: $warn; text-style: bold;
    }
```

(The existing `Vertical` fills the screen, so `align` cannot move it;
only the auto-sized `#modal` centers. If layer alignment misbehaves in
Textual 8.2.7, fallback: make `#modal` full-screen with
`content-align: center middle;` and keep its background unset so the
box text carries its own chrome.)

2. `compose()`: after `yield Static(KEYBAR, id="keybar")` add
   `yield Static("", id="modal", markup=False)`.

3. `__init__`: `self._modal_open = False` next to the other flags.

4. `_any_overlay_open()`: add `or self._modal_open` to the return.

5. New methods next to `_extreme_form_close`:

```python
    def _modal_show(self, title: str, lines: list) -> None:
        """Float a DOS-style dialog over the UI until any key closes it.
        Display-only: callers keep their log lines - the modal is
        visibility, the log is history."""
        m = self.query_one("#modal", Static)
        w = max(40, self.size.width - 4)
        m.update(dos_modal_text(title, lines, w))
        m.styles.display = "block"
        self._modal_open = True

    def _modal_close(self) -> None:
        self._modal_open = False
        try:
            self.query_one("#modal", Static).styles.display = "none"
        except Exception:
            pass
```

6. `on_key()`: at the very top, before the timers-overlay early return:

```python
        if self._modal_open:
            self._modal_close()
            event.stop()
            event.prevent_default()
            return
```

The timers comment (~line 1750) documents that `event.stop()` alone did
NOT stop app bindings in the past; `prevent_default()` is what suppresses
the binding. The pilot test's point 2 (`q` must not arm quit) is the
arbiter: if `prevent_default()` proves insufficient in 8.2.7, add
`if self._modal_open: return` at the top of the unguarded actions the
test exercises (`action_quit`, `action_help`, `action_swarm_view`,
`action_settings`) - the guarded ones already go through
`_any_overlay_open()`.

7. `action_dismiss_view`: before the extreme-form check, add:

```python
        if self._modal_open:
            self._modal_close()
            return
```

8. `action_extreme` refusal branch: after the existing
   `log.write_line("extreme: requires INSANE first ...")` line, add:

```python
            self._modal_show(
                "EXTREME - NOT AVAILABLE",
                ["This session is not INSANE.",
                 "SPACE cycles the arm level to",
                 "✦ INSANE, then press E E again."])
```

Keep the refusal's existing arm-state clearing untouched.

- [ ] **Step 4: Run to verify pass, then everything**

Run: `python3 iterm/test_extreme.py`, `python3 iterm/test_app.py`, then
`./test/run.sh`
Expected: `ALL PASSED` / `ALL PASS` / `ALL SUITES PASSED`.

- [ ] **Step 5: Commit**

```bash
git add iterm/app.py iterm/test_extreme.py
git commit -m "feat(tui): DOS modal floats the E E refusal - any key dismisses"
```
