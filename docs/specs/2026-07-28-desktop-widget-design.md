# Desktop Widget (floating mascot) - Design Spec

**Date:** 2026-07-28
**Status:** Approved for planning

## Summary

A small always-on-top desktop window showing relay's mascot - the same creature,
the same mood, the same speech - so you can be in a browser, Figma or Slack and
still know relay is alive and that nothing needs you. Read-only: it displays,
it never acts.

Origin: operator request ("almost like Clippy from the old MS Office days").

The problem it solves is specific. relay's entire pitch is *walk away and trust
it*, but today walking away means the panel is behind six windows and you cannot
see the one thing it exists to tell you. The `⚫ RELAY off` status-bar badge is
per-tab and only visible while iTerm2 is frontmost. Notifications are discrete
events; the mascot is continuous state. Nothing currently carries continuous
state out of the terminal.

## 1. What it is not

- **Not interactive.** No pause button, no arm toggle, no click targets. A
  floating always-on-top window that can pause your supervisor is an accident
  waiting to happen, and it would undercut the "a physical click is
  un-spoofable" property the status bar deliberately relies on. The reverse
  channel (`statusbar-clicks.jsonl`) exists and could carry actions later; this
  build does not use it.
- **Not a second source of truth.** It renders what relay publishes and derives
  nothing.
- **Not a daemon.** relay owns its lifetime (see §6).

## 2. Architecture

The publish/poll pattern `iterm/statusbar.py` already proves in production:

```
relay TUI (app.py)                        widget (Tauri)
  every reactor tick                        every ~1s
  widget.write_state(payload)  ---->  ~/.relay/widget.json  ---->  poll + render
  atomic: tmp + os.replace                  ts older than 5s = "relay off"
```

One-way. No socket, no IPC, no handshake. A torn read is impossible because the
write is an `os.replace`. If relay is not running the file is stale or absent
and the widget says so, exactly as `OFFLINE_LABEL` does for the badge.

## 3. The contract

`~/.relay/widget.json` (override: `RELAY_WIDGET_STATE`):

```json
{ "ts": 1753700000.1,
  "state": "guarding",
  "color": "#6effa0",
  "art": ["  _____ ", " (o   o)", "  \\___/ "],
  "armed": 3, "awaiting": 0, "working": false, "paused": false,
  "band": "calm", "sessions": 7 }
```

**Both halves cross the boundary, and they do different jobs:**

- `art` is the mascot block **already rendered** by `mascot_face_big`, speech
  bubble included. The widget draws it in a monospace `<pre>` and knows nothing
  about mascots. This is what makes all 15 skins and all 8 states work on day
  one, keeps a new skin a zero-change event for the widget, and makes parity
  with the TUI a property of the design rather than a thing to remember.
- `state` / `color` / the counts drive the **frame**: window tint, glow, scale,
  the alarm pulse. This is what stops it reading as a terminal in a box.

`state` is one of the eight from `effective_mascot_state`: `alarmed`,
`critical`, `working`, `guarding`, `idle`, `paused`, `flinch`, `celebrate`.

**Deliberately NOT published:** the speech phrase as a separate field. It is
already inside `art`, and extracting it would mean refactoring
`mascot_face_big`'s bubble assembly for no gain in this build.

**Staleness:** `STALE_S = 5.0`, matching `statusbar.STATE_STALE_S` - longer than
the 2s watcher tick, shorter than human patience.

## 4. relay side: `iterm/widget.py`

One new pure module, in the mould of `gates.py` / `statusbar.py` / `timers.py`:
no `iterm2` import, no Tauri knowledge, no Textual, unit-testable standalone.

- `state_path() -> str`
- `payload(state, color, art, *, armed, awaiting, working, paused, band, sessions, now=None) -> dict`
- `write_state(payload, path=None) -> None` - atomic tmp + `os.replace`
- `clear_state(path=None) -> None` - best-effort unlink on quit
- `state_fresh(now=None, path=None) -> bool` - for tests and `relay doctor`

**Hook point:** `banner_with_face` (`app.py:521`) already computes `state`,
`color = _MASCOT_COLOR[state]`, and `face = mascot_face_big(...)`. The publisher
takes those three exactly as computed, so the widget can never disagree with the
banner. Note `banner_with_face` returns a markup-joined string - the publisher
must capture `face` **before** the markup join, so the widget gets clean text.

**Config:** `[widget] enabled = false` in `~/.relay/config`, default **off**
because enabling it launches a process. Joins `config.Config` and
`settings.SETTINGS` as a `toggle`, exactly as `sounds_enabled` did. It is NOT a
`_LIVE` setting: toggling it starts or stops a real process, so it takes effect
on the next relay start rather than mid-run.

## 5. Widget side: `widget/`

A `widget/` directory at the repo root - namespaced deliberately. A bare
`src-tauri/` at top level would misrepresent a repo whose install story is
"symlink a bash script".

Structure mirrors glassamp (verified working reference, `~/Work/glassamp`):

```
widget/
  src/index.html          # everything: markup, CSS, ~150 lines of JS. No framework.
  src-tauri/
    tauri.conf.json
    Cargo.toml
    src/main.rs
```

**Window:** ~260x180, `decorations: false`, `transparent: true`,
`alwaysOnTop: true`, `macOSPrivateApi: true` (required for transparency),
`skipTaskbar: true`, `resizable: false`. The body carries
`data-tauri-drag-region` plus `appWindow.startDragging()` on mousedown - the
exact pattern glassamp uses - so it is flung anywhere with a drag and needs no
title bar.

**Reading state:** `tauri-plugin-fs` with its scope restricted to the single
path `$HOME/.relay/widget.json`. Poll at 1s. Any read failure, parse failure or
`ts` older than `STALE_S` renders the offline face.

**Quitting:** a tray icon (Tauri's `tray-icon` feature, already a glassamp
dependency) with a single Quit item. A chrome-less window has no close button
and must not be a trap.

**Position:** persisted across launches so the creature returns where you left
it.

## 6. Lifecycle

**relay owns it.** On start, if `[widget] enabled`, relay launches the widget;
on quit (`q`) relay terminates it.

This is not a stylistic preference. The README promises *"One process: tool on
=== TUI open; quit (q) === everything stops. No daemon, no auto-launch."* A
widget you start and stop yourself breaks that quietly, and worse, leaves a
creature on screen cheerfully reporting a relay that died - the exact failure
`OFFLINE_LABEL` was invented to prevent. The status-bar AutoLaunch provider is
the one existing exception, and it exists only because iTerm2 renders a missing
provider as an ERROR and forces the provider to outlive relay. There is no such
forcing function here, so we do not create a second exception by choice.

The widget still renders an offline face if it somehow outlives relay - belt and
braces, since the staleness check costs nothing.

## 7. Risks

1. **Always-on-top does not automatically mean "above a fullscreen app" on
   macOS.** `tao-0.34.5`'s `set_visible_on_all_workspaces` sets
   `NSWindowCollectionBehavior::CanJoinAllSpaces` but **not**
   `FullScreenAuxiliary` (verified by reading
   `tao-0.34.5/src/platform_impl/macos/window.rs:1534`). Combined with the
   raised window level from `alwaysOnTop` this is very likely sufficient, but
   "very likely" is not good enough for the single property the feature lives or
   dies on: if you work in a fullscreen browser all day and the widget is not
   there, the feature is pointless. **This is Task 1 of the plan and everything
   else is gated on it.** If it fails, the fallback is a menu-bar tray item
   (`tray-icon` is already in the dependency set) - which cannot show the
   creature, and at that point the feature should be reconsidered rather than
   downgraded silently.
2. **The "one process" claim in the README stops being literally true** and must
   be reworded, whatever else happens.
3. **The `.app` will be unsigned**, so macOS Gatekeeper will block it on any
   machine that is not the build machine. Acceptable for personal use; a real
   problem the day relay ships to anyone else. Not solved here, named here.
4. **A Rust toolchain becomes required to build from source.** `cargo 1.92.0` is
   present on the dev machine. `./install.sh` must degrade gracefully when it is
   absent: skip the widget, say so, install everything else.

## 8. Testing

- `iterm/test_widget.py` - table-driven over all eight states; payload shape;
  atomic write leaves no `.tmp` behind; `clear_state` on a missing file does not
  raise; `state_fresh` at the staleness boundary. Same style as `test_gates.py`,
  runs inside `./test/run.sh`.
- `test_config.py` / `test_settings.py` - the `[widget] enabled` key parses,
  round-trips, rejects junk, and renders in the settings editor.
- `test_app.py` - the publisher is called with the same `state`/`color`/`art`
  the banner renders, and is a no-op when the config toggle is off.
- **The Tauri half gets no automated tests, deliberately and explicitly.** It is
  one HTML file whose only real risk is window behaviour, which no headless
  runner can assert. The §7.1 spike covers that risk directly. `./test/run.sh`
  stays Python-only and does not grow a second toolchain; building the widget is
  a separate script.

## 9. Out of scope

- Any interactivity (pause, arm, click-to-focus). The reverse channel exists;
  this build does not use it.
- Hand-drawn or animated mascot artwork. The `state`/`color` fields are
  published so a hero skin can be added later for one creature without
  disturbing the other fourteen, but no such artwork is built here.
- Windows and Linux. relay is macOS-only (iTerm2).
- Code signing and notarization (see §7.3).
- Per-session detail in the widget. It shows the fleet's mood, not a session
  list - that is what the panel is for.
