# TUI Config Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A keyboard-driven Settings overlay in the panel to edit `~/.relay/config` - sounds apply live (with a play-sample), the rest writes with a restart note.

**Architecture:** A pure config writer (`config.dump`/`save`), a pure settings model (`settings.py`: descriptors + reducer + renderer), and TUI wiring (`app.py`: a `#settingsview` overlay toggled by `,`, arrow-key editing, auto-apply + auto-save). Plus a docs pass covering this session's features.

**Tech Stack:** Python 3 (stdlib only), Textual TUI. Tests: `python3 iterm/test_*.py`; whole suite `./test/run.sh`.

**Spec:** `docs/specs/2026-07-22-config-editor-design.md`

## Global Constraints

- **No em-dash** (U+2014) or `&mdash;` anywhere - source, strings, comments, commit messages. ASCII hyphen `-`.
- **No `Co-Authored-By` trailer** in commit messages.
- Tests: no pytest. `python3 iterm/test_<name>.py`; whole suite `./test/run.sh`.
- Keyboard-only UI (no Textual form widgets); the overlay renders as plain CRT-style text in a Static.
- Only the four `*_sound` fields apply live; everything else is write + a restart note.
- `Config` is `@dataclass(frozen=True)` - build new instances with `dataclasses.replace`, never mutate.
- Auto-save writes must be atomic (tmp + `os.replace`) and best-effort (never crash the panel).

---

### Task 1: Config writer (`dump` / `save`)

**Files:**
- Modify: `iterm/config.py` (add `dump`/`save` after `load`)
- Test: `iterm/test_config.py`

**Interfaces:**
- Produces: `config.dump(cfg: Config) -> str`; `config.save(cfg: Config, path=None) -> None`. Round-trip: `load(<file save wrote>)[0] == cfg`.

- [ ] **Step 1: Write the failing test**

Add to `iterm/test_config.py` inside `run()`:

```python
    import dataclasses
    # dump -> load round-trips every managed field (non-default values).
    custom = dataclasses.replace(
        config.Config(), title_style="hybrid", alert_sound="/a/x.aiff",
        done_sound="", danger_sound="/a/d.aiff", message_sound="/a/m.aiff",
        stale_minutes=7.0, notify_cooldown=15.0, spawn_arm="wild",
        statusbar_enabled=True, danger_preset="paranoid", theme="amber")
    p = _write(config.dump(custom))
    back, warns = config.load(p)
    ok &= check("dump->load round-trips every field", back == custom)
    ok &= check("round-trip has no warnings", warns == [])
    # save writes atomically to the given path.
    import tempfile, os as _os
    sp = _os.path.join(tempfile.mkdtemp(), "cfg")
    config.save(custom, sp)
    ok &= check("save then load equals cfg", config.load(sp)[0] == custom)
    ok &= check("silent sound round-trips as empty", back.done_sound == "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_config.py`
Expected: FAIL - `module 'config' has no attribute 'dump'`.

- [ ] **Step 3: Implement dump + save**

In `iterm/config.py`, add after `load`:

```python
def dump(cfg: Config) -> str:
    """Render a complete ~/.relay/config from a Config. Round-trips: load() of
    this text yields an equal Config for every field the editor manages."""
    return (
        "; Written by relay's config editor. Edit here or in the panel (,).\n\n"
        "[titles]\n"
        f"style = {cfg.title_style}\n\n"
        "[sounds]\n"
        f"alert   = {cfg.alert_sound}\n"
        f"done    = {cfg.done_sound}\n"
        f"danger  = {cfg.danger_sound}\n"
        f"message = {cfg.message_sound}\n\n"
        "[swarm]\n"
        f"stale_minutes   = {cfg.stale_minutes:g}\n"
        f"notify_cooldown = {cfg.notify_cooldown:g}\n"
        f"spawn_arm       = {cfg.spawn_arm}\n\n"
        "[statusbar]\n"
        f"enabled = {'true' if cfg.statusbar_enabled else 'false'}\n\n"
        "[danger]\n"
        f"preset = {cfg.danger_preset}\n\n"
        "[theme]\n"
        f"name = {cfg.theme}\n"
    )


def save(cfg: Config, path: Optional[str] = None) -> None:
    """Atomically write dump(cfg) to path (default default_path())."""
    p = path or default_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(dump(cfg))
    os.replace(tmp, p)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_config.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add iterm/config.py iterm/test_config.py
git commit -m "feat(config): dump/save - write ~/.relay/config from a Config, round-trips load"
```

---

### Task 2: Settings model (pure `settings.py`)

**Files:**
- Create: `iterm/settings.py`
- Create: `iterm/test_settings.py`

**Interfaces:**
- Produces: `settings.SETTINGS` (ordered descriptors); `settings.is_live(field) -> bool`; `settings.sound_options(current) -> list`; `settings.change(cfg, field, direction) -> Config`; `settings.render(working, running, cursor, width) -> str`.

- [ ] **Step 1: Write the failing test**

Create `iterm/test_settings.py`:

```python
"""Tests for the pure settings model (config editor). No Textual/iTerm2.

Run: python3 iterm/test_settings.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402
import settings  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    c = config.Config()

    ok &= check("is_live only for sounds",
                settings.is_live("alert_sound")
                and not settings.is_live("theme"))

    # enum cycles and wraps
    t = settings.change(c, "theme", +1).theme
    ok &= check("enum cycles to next", t == config.THEME_NAMES[1])
    ok &= check("enum wraps on left from first",
                settings.change(c, "theme", -1).theme == config.THEME_NAMES[-1])

    # toggle flips
    ok &= check("toggle flips",
                settings.change(c, "statusbar_enabled", +1).statusbar_enabled
                is (not c.statusbar_enabled))

    # number steps and respects min
    ok &= check("number steps up",
                settings.change(c, "notify_cooldown", +1).notify_cooldown
                == c.notify_cooldown + 5.0)
    lowered = config.Config()
    import dataclasses
    lowered = dataclasses.replace(lowered, stale_minutes=1.0)
    ok &= check("number clamps at min",
                settings.change(lowered, "stale_minutes", -1).stale_minutes
                == 1.0)

    # sound options include silent + a custom current
    opts = settings.sound_options("/my/custom.aiff")
    ok &= check("sound options include silent + custom",
                "" in opts and "/my/custom.aiff" in opts)

    # unknown field is a no-op
    ok &= check("unknown field no-op", settings.change(c, "nope", +1) == c)

    # render shows cursor + a restart tag only on a changed restart field
    changed = settings.change(c, "theme", +1)
    txt = settings.render(changed, c, 0, 60)
    ok &= check("render marks the cursor row", ">" in txt)
    ok &= check("render shows restart tag on changed restart field",
                "restart" in txt)
    live_changed = settings.change(c, "alert_sound", +1)
    txt2 = settings.render(live_changed, c, 0, 60)
    ok &= check("no restart tag for a live (sound) change",
                "restart" not in txt2)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_settings.py`
Expected: FAIL - `No module named 'settings'`.

- [ ] **Step 3: Implement settings.py**

Create `iterm/settings.py`:

```python
"""Pure settings model for the TUI config editor. No Textual, no iTerm2 - like
titles.py / statusbar.py. One ordered descriptor list drives both the rendered
overlay and the arrow-key editing, so they cannot drift."""
import dataclasses
import glob
import os

import config as _config

SYSTEM_SOUNDS_DIR = "/System/Library/Sounds"

# (group, field, kind, spec). kind: sound | enum | toggle | number.
#   enum   -> spec is the tuple of valid values
#   number -> spec is (min, step)
#   toggle -> spec None
#   sound  -> spec None (options are dynamic, see sound_options)
SETTINGS = [
    ("SOUNDS", "alert_sound", "sound", None),
    ("SOUNDS", "done_sound", "sound", None),
    ("SOUNDS", "danger_sound", "sound", None),
    ("SOUNDS", "message_sound", "sound", None),
    ("APPEARANCE", "theme", "enum", _config.THEME_NAMES),
    ("APPEARANCE", "title_style", "enum", _config.TITLE_STYLES),
    ("BEHAVIOR", "statusbar_enabled", "toggle", None),
    ("BEHAVIOR", "spawn_arm", "enum", _config.SPAWN_ARM_MODES),
    ("BEHAVIOR", "stale_minutes", "number", (1.0, 1.0)),
    ("BEHAVIOR", "notify_cooldown", "number", (0.0, 5.0)),
    ("BEHAVIOR", "danger_preset", "enum", _config.DANGER_PRESETS),
]

_LIVE = {"alert_sound", "done_sound", "danger_sound", "message_sound"}


def is_live(field: str) -> bool:
    return field in _LIVE


def _descriptor(field):
    for row in SETTINGS:
        if row[1] == field:
            return row
    return None


def sound_options(current: str) -> list:
    """System sounds + '' (silent) + the current value if it is a custom path.
    Never raises."""
    try:
        found = sorted(glob.glob(os.path.join(SYSTEM_SOUNDS_DIR, "*.aiff")))
    except Exception:
        found = []
    opts = [""] + found
    if current and current not in opts:
        opts.append(current)
    return opts


def _cycle(options, current, direction):
    if not options:
        return current
    try:
        i = options.index(current)
    except ValueError:
        return options[0] if direction >= 0 else options[-1]
    return options[(i + direction) % len(options)]


def change(cfg, field, direction):
    """A NEW frozen Config with `field` cycled/stepped by direction (+1 right,
    -1 left). Unknown field -> cfg unchanged. Pure."""
    d = _descriptor(field)
    if d is None:
        return cfg
    _, _, kind, spec = d
    cur = getattr(cfg, field)
    if kind == "toggle":
        new = not cur
    elif kind == "enum":
        new = _cycle(list(spec), cur, direction)
    elif kind == "sound":
        new = _cycle(sound_options(cur), cur, direction)
    elif kind == "number":
        lo, step = spec
        new = max(lo, cur + direction * step)
    else:
        return cfg
    return dataclasses.replace(cfg, **{field: new})


def _display(kind, value):
    if kind == "toggle":
        return "on" if value else "off"
    if kind == "sound":
        return "(silent)" if not value else os.path.basename(value)
    if kind == "number":
        return f"{value:g}"
    return str(value)


def render(working, running, cursor, width):
    """The settings overlay text (plain, CRT style). Marks the cursor row and
    tags a changed restart-field with 'restart to apply'. Pure."""
    lines = []
    group = None
    for i, (g, f, kind, spec) in enumerate(SETTINGS):
        if g != group:
            group = g
            lines.append("")
            lines.append(f"  {g}")
        mark = ">" if i == cursor else " "
        val = _display(kind, getattr(working, f))
        tag = ""
        if not is_live(f) and getattr(working, f) != getattr(running, f):
            tag = "   restart to apply"
        label = f.replace("_", " ")
        lines.append(f" {mark} {label:<18} {val}{tag}")
    lines.append("")
    lines.append("  up/down move   left/right change   p play sound   , close")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_settings.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add iterm/settings.py iterm/test_settings.py
git commit -m "feat(settings): pure config-editor model - descriptors, reducer, renderer"
```

---

### Task 3: TUI wiring (overlay, keys, live-apply, auto-save)

**Files:**
- Modify: `iterm/app.py` (imports; `BINDINGS`; `KEYBAR`; `help_text`; `compose`; overlay CSS; `__init__` state; `action_settings`; key routing in `action_cursor_up`/`action_cursor_down`/`action_pause`; new `action_settings_left`/`action_settings_right`; `action_dismiss_view`; a `_render_settings` helper)
- Test: `iterm/test_app.py`

**Interfaces:**
- Consumes: `config.save`, `settings.SETTINGS`/`change`/`render`/`is_live` (Tasks 1-2); `Watcher` sound attributes.

- [ ] **Step 1: Write the failing pilot test**

Add to `iterm/test_app.py` inside `go()` (uses the `_TestApp`/pilot pattern already in the file):

```python
    # --- config editor overlay -------------------------------------------
    ce = _TestApp(_one(), dry_run=True)
    async with ce.run_test() as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        chk("comma opens settings",
            ce._settings_visible
            and str(ce.query_one("#settingsview").styles.display) == "block")
        # move to the first sound row (cursor starts at 0 = alert_sound), change
        before = ce.watcher.alert_sound
        await pilot.press("right")
        await pilot.pause()
        chk("right on a sound row changes the live watcher sound",
            ce.watcher.alert_sound != before)
        await pilot.press("comma")
        await pilot.pause()
        chk("comma closes settings", not ce._settings_visible)
    chk("KEYBAR advertises settings", "," in appmod.KEYBAR
        and "settings" in appmod.KEYBAR.lower())
    chk("help covers settings", "settings" in appmod.help_text().lower())
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 iterm/test_app.py`
Expected: FAIL - `'_TestApp' object has no attribute '_settings_visible'` / no `comma` binding.

- [ ] **Step 3: Wire the overlay**

In `iterm/app.py`:

(a) Import the modules near the other imports:
```python
import config as cfgmod
import settings as settingsmod
```
(If `config` is already imported under another name, reuse it; the watcher passes its `Config` in - see (e).)

(b) `compose` - add the overlay Static next to `#swarmview`/`#helpview`:
```python
            yield Static("", id="settingsview")
```

(c) Overlay CSS - add `#settingsview` to the existing group:
```python
    #swarmview, #helpview, #settingsview {
        display: none; height: 1fr; padding: 0 2;
        background: $bg_deep; color: $accent;
    }
```

(d) `BINDINGS` - add (near the `s` binding):
```python
        Binding("comma", "settings", "Settings"),
        Binding("left", "settings_left", "Change", show=False),
        Binding("right", "settings_right", "Change", show=False),
```

(e) `__init__` state (near the other `_*_visible` flags):
```python
        self._settings_visible = False
        self._settings_cursor = 0
        self._running_cfg = self.watcher.cfg      # restart baseline
        self._working_cfg = self.watcher.cfg
```
(If `self.watcher` is not yet set in `__init__`, initialize `_running_cfg`/`_working_cfg` to `None` here and set them in `on_mount`/wherever the watcher is attached; guard `_render_settings` against `None`.)

(f) `KEYBAR` - add `,` settings to the key bar string (match the existing format).

(g) `help_text` - add a `,` row (Open the settings editor) in the keys section.

(h) The actions and render helper (near `action_help`):
```python
    def action_settings(self) -> None:
        if self._swarm_visible and not self._settings_visible:
            self.action_swarm_view()      # leave swarm first
        self._settings_visible = not self._settings_visible
        on = self._settings_visible
        self.query_one("#middle").styles.display = "none" if on else "block"
        self.query_one("#log").styles.display = "none" if on else "block"
        self.query_one("#settingsview").styles.display = "block" if on else "none"
        if on:
            self._render_settings()

    def _render_settings(self) -> None:
        if self._working_cfg is None:
            return
        w = self.query_one("#settingsview").size.width - 4
        self.query_one("#settingsview", Static).update(
            settingsmod.render(self._working_cfg, self._running_cfg,
                               self._settings_cursor, max(40, w)))

    def _settings_move(self, step: int) -> None:
        n = len(settingsmod.SETTINGS)
        self._settings_cursor = (self._settings_cursor + step) % n
        self._render_settings()

    def _settings_change(self, direction: int) -> None:
        field = settingsmod.SETTINGS[self._settings_cursor][1]
        self._working_cfg = settingsmod.change(self._working_cfg, field,
                                               direction)
        if settingsmod.is_live(field) and self.watcher:
            setattr(self.watcher, field, getattr(self._working_cfg, field))
        try:
            cfgmod.save(self._working_cfg)
        except Exception as e:
            self.query_one(Log).write_line(f"config save failed: {e}")
        self._render_settings()

    def _settings_play(self) -> None:
        field = settingsmod.SETTINGS[self._settings_cursor][1]
        if not settingsmod.is_live(field):
            return
        path = getattr(self._working_cfg, field)
        if path:
            import subprocess
            try:
                subprocess.Popen(["afplay", path],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def action_settings_left(self) -> None:
        if self._settings_visible:
            self._settings_change(-1)

    def action_settings_right(self) -> None:
        if self._settings_visible:
            self._settings_change(+1)
```

(i) Route the overlapping keys - at the TOP of the existing methods:
```python
    def action_cursor_up(self) -> None:
        if self._settings_visible:
            self._settings_move(-1)
            return
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        if self._settings_visible:
            self._settings_move(+1)
            return
        self._move_cursor(+1)
```
And in `action_pause`, add at the top:
```python
    def action_pause(self) -> None:
        if self._settings_visible:
            self._settings_play()
            return
        if self.watcher:
            self.watcher.toggle_pause()
            self._refresh()
```

(j) `action_dismiss_view` - add settings to the escape chain (before or after the others):
```python
        if self._settings_visible:
            self.action_settings()
        elif self._help_visible:
            ...
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 iterm/test_app.py`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`.

- [ ] **Step 6: Commit**

```bash
git add iterm/app.py iterm/test_app.py
git commit -m "feat(tui): config editor overlay - comma opens settings, arrows edit, live sounds, auto-save"
```

---

### Task 4: Documentation pass (this session's features)

**Files:**
- Modify: `README.md`

**Context:** The README documents the status-bar badge and tab titles but NOT the features shipped this session. Bring it current.

- [ ] **Step 1: Update the key reference**

Find the key list(s) in `README.md` (search for `SPACE arm`, `TAB swarm`, or the "Keys" section). Add rows for the new bindings, matching the existing format:
- `p` - pause / resume relay's acting (freezes approvals + deliveries, keeps watching)
- `s` - shadow-arm the selected tab (dry-run: shows what it would do, never acts)
- `,` - open the settings editor

- [ ] **Step 2: Add a "Pause and shadow" subsection**

Under the arm-levels / how-it-works area, add a short subsection:

```markdown
### Pause and shadow (reversible controls)

- **Pause (`p`)** freezes relay's *hands* - it stops auto-approving and stops
  delivering swarm messages - while keeping its *eyes*: it still watches, shows
  live state, and pings you on danger. It holds until you press `p` again (no
  auto-resume), and the panel shows a loud `PAUSED` banner plus a frozen mascot
  so you can never mistake a paused relay for an armed one.
- **Shadow-arm (`s`)** is a per-tab dry-run: relay classifies the tab's prompts
  with the *safe* rules and records what it *would* do (`WOULD CLEAR` /
  `WOULD ESCALATE`) without ever acting - so you can trust-test one new tab
  while your other armed tabs keep running. A shadow tab shows a hollow `◌`
  badge (blue circle in the status bar).
```

- [ ] **Step 3: Add a "Sounds and the settings editor" subsection**

```markdown
### Sounds and the settings editor

Relay uses four distinct sounds so your ear can triage without looking, all set
in `[sounds]` (any can be `` empty to silence it):

| Key | Fires on | Default |
|-----|----------|---------|
| `danger` | a session about to run a dangerous command | Basso |
| `alert` | needs a look (real question, stale session, error) | Sosumi |
| `message` | a swarm worker messaged / escalated to you | Tink |
| `done` | a task or epic completed | Glass |

Press **`,`** in the panel to open the **settings editor**: arrow keys move and
change each setting, `p` auditions the highlighted sound, and changes save to
`~/.relay/config` as you go. Sound changes apply immediately; the rest take
effect on the next relay start (the editor tags those with a restart note).
On Apple Silicon the status-bar badge also needs Rosetta 2 - `relay doctor`
checks it.
```

- [ ] **Step 4: Add `relay recap` to the CLI/examples**

Find where CLI subcommands are documented (search for `relay doctor` or `relay spawn`). Add:

```markdown
### See what relay did

`relay recap` prints a one-line summary of today's activity (cleared N, woke you
M times, tasks done); `relay recap --all` covers all time. The panel also prints
this line when you quit.
```

- [ ] **Step 5: Verify no em-dash and the doc reads cleanly**

Run: `grep -nP '\x{2014}' README.md && echo FOUND || echo clean`
Expected: `clean`.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs(readme): document pause, shadow, sounds, settings editor, and recap"
```

---

## Self-Review

**1. Spec coverage:** config writer (Task 1); pure settings model - descriptors, reducer, renderer, sound options, live flag (Task 2); overlay + `,` + arrow editing + live-apply + auto-save (Task 3); docs (Task 4, plus spec-adjacent session features). ✓

**2. Placeholder scan:** every code step shows full code; tests show assertions. The Task 3 `(a)`/`(e)` parenthetical fallbacks are conditional guidance for the implementer to reconcile against the real file, not placeholders. ✓

**3. Type consistency:** `settings.change`/`render`/`is_live`/`SETTINGS`/`sound_options` names match across Task 2 (define) and Task 3 (consume); `config.save`/`dump` match across Task 1 and Task 3; `_settings_visible`/`_working_cfg`/`_running_cfg`/`_settings_cursor` are consistent within Task 3. ✓
