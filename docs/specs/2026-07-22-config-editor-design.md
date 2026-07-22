# TUI Config Editor - design

**Date:** 2026-07-22
**Status:** approved (brainstorm), pending implementation plan
**Scope:** First piece of Spec 3 (Frictionless setup & settings). The other two
pieces - `relay doctor --fix` and guided first-run - are separate, later specs.

## Why

Relay's preferences live in `~/.relay/config` (INI). Editing them means quitting
the panel, hand-editing a dotfile, and restarting - and most users never
discover the knobs exist (the sound keys, themes, arm defaults). A Settings
screen in the panel makes the config discoverable and editable in place, with a
**play-sample** button that turns sound selection from guesswork into
audition - the payoff the `[sounds]` config work set up.

## Decisions (from brainstorm)

- **Apply timing:** sounds apply **live** (the watcher holds the four sound
  paths; updating them takes effect on the next notification, and `p` auditions
  instantly). Everything else **writes the file + shows a restart note** - live
  theming would require making the baked-in color constants and class-level CSS
  dynamic, a separate refactor out of scope here.
- **UI:** a keyboard-driven overlay rendered in the panel's own CRT style (not
  standard Textual form widgets, which clash with the hand-rendered aesthetic).
- **Interaction:** uniform and arrow-driven - `←/→` changes every field
  (enums cycle, toggle flips, numbers step); no typing mode.
- **Save model:** auto-apply + auto-save (no dirty buffer, no explicit save, no
  cancel - config changes are low-stakes and self-reversible).

## The config surface

All keys in `config.Config` are editable (small surface, fully covered), grouped
for display:

| Group | Field | Type | Values / step | Live? |
|-------|-------|------|---------------|-------|
| Sounds | `alert_sound` | sound | system sounds + `(silent)` (+ current custom) | **live** |
| Sounds | `done_sound` | sound | " | **live** |
| Sounds | `danger_sound` | sound | " | **live** |
| Sounds | `message_sound` | sound | " | **live** |
| Appearance | `theme` | enum | `THEME_NAMES` (phosphor/amber/ice) | restart |
| Appearance | `title_style` | enum | `TITLE_STYLES` (off/glyphs/words/hybrid) | restart |
| Behavior | `statusbar_enabled` | toggle | true/false | restart |
| Behavior | `spawn_arm` | enum | `SPAWN_ARM_MODES` (off/safe/wild/insane) | restart |
| Behavior | `stale_minutes` | number | min 1, step 1 | restart |
| Behavior | `notify_cooldown` | number | min 0, step 5 | restart |
| Behavior | `danger_preset` | enum | `DANGER_PRESETS` (default/paranoid) | restart |

## Architecture

Three layers, most of it pure and iTerm2/Textual-free (testable like `titles.py`
/ `statusbar.py`):

### 1. Config writer (new, in `config.py`)

- `dump(cfg: Config) -> str` - render a complete, INI with section headers and
  brief inline comments, from the `Config` values. Deterministic.
- `save(cfg: Config, path=None) -> None` - atomic write (tmp + `os.replace`),
  `path` defaults to `default_path()`. Never partially writes.
- Round-trip guarantee: `load(save-target) == cfg` for every field the editor
  manages. The editor becomes the file's source of truth, so hand-written
  comments are not preserved (acceptable).

### 2. Settings model (new module `settings.py`, pure)

- `SETTINGS: list` of field descriptors - `(group, field, kind, spec)` where
  `kind in {"sound","enum","toggle","number"}` and `spec` carries the
  options / (min, step) it needs. One ordered list drives both rendering and
  editing, so they can never drift.
- `sound_options(current: str) -> list[str]` - sorted `/System/Library/Sounds/
  *.aiff` paths, plus `""` (silent), plus `current` if it is a non-system
  custom path (so a user's custom sound is never dropped). Never raises (a
  missing sounds dir yields just `["", current]`).
- `change(cfg: Config, field: str, direction: int) -> Config` - the pure
  reducer: return a NEW frozen `Config` with `field` cycled/stepped by
  `direction` (+1 for `→`, -1 for `←`), respecting bounds and wrap-around.
  Uses `dataclasses.replace`.
- `render(working: Config, running: Config, cursor: int, width: int) -> str` -
  the overlay text: grouped rows, the cursor row marked, each row's current
  value, and a `↻ restart` tag on a restart-field whose `working` value differs
  from `running` (the config relay started with). Plain text (CRT style).
- `is_live(field: str) -> bool` - True only for the four `*_sound` fields.

### 3. TUI wiring (`app.py`)

- `#settingsview` Static overlay + `_settings_visible` flag + cursor index,
  toggled by `,` (mirrors `action_help`/`action_swarm_view`: hide
  `#middle`/`#log`, show `#settingsview`).
- State: `self._running_cfg` (the `Config` relay started with - the restart
  baseline, set once) and `self._working_cfg` (edited live).
- `action_settings` (`,`): open/close. While open, key handling routes
  `up/down` -> move cursor, `left/right` -> `change(...)`, `p` -> audition the
  cursor row's sound. (Inside the overlay these keys are consumed by the
  settings handler, not the global arm/pause bindings.)
- On every `change`: update `_working_cfg`; if `is_live(field)`, push the new
  value onto the running watcher (`self.watcher.<field> = value`); then
  `config.save(self._working_cfg)`.
- `p` audition: `subprocess.Popen(["afplay", path])` (best-effort, never
  raises; `(silent)` plays nothing).

## Interaction summary

```
,   open / close Settings          up/down  move between settings
left/right  change the setting      p        play the selected sound
Esc close                           (auto-saves + applies on every change)
```

## Testing

- **`config.dump`/`save`:** `load(save(cfg)) == cfg` for a cfg with every field
  set to a non-default; save is atomic (tmp path); a `(silent)` sound
  round-trips as an empty value.
- **`settings.change`:** each kind cycles/steps correctly and wraps; numbers
  respect their min and step; a toggle flips; unknown field is a no-op.
- **`settings.sound_options`:** includes silent + a custom current path;
  missing sounds dir degrades to `["", current]` without raising.
- **`settings.render`:** shows the cursor, values, and the `↻ restart` tag only
  on a changed restart-field (never on a sound field).
- **Pilot (`test_app.py`):** `,` opens/closes the overlay; a `←/→` on a sound
  row updates `watcher.alert_sound` and writes the file; a `←/→` on a restart
  field shows the `↻` tag but does NOT change the running watcher.

## Rollout / risk

- Additive: a new overlay + a new pure module + two `config.py` functions. No
  change to how config is loaded or to the watcher's act path.
- Auto-save writes `~/.relay/config` on each change - low-risk (atomic write,
  always-valid values), and the file is small. A write failure is best-effort
  logged, never crashes the panel.
- Sounds live-apply is a plain attribute set on the watcher; a bad value at
  worst plays nothing (afplay no-ops).
- The editor only ever offers valid values, so it can never write an invalid
  config that `load()` would warn about.

## Out of scope (later Spec-3 pieces)

1. **`relay doctor --fix`** - auto-remediate detected setup issues.
2. **Guided first-run** - onboarding that shows a safe auto-approval live.
3. **Live theming** - applying a theme change without a restart.
