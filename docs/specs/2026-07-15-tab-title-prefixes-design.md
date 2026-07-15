# Tab-Title Status Prefixes + Config File - Design Spec

**Date:** 2026-07-15
**Status:** Approved for planning

## Summary

Relay rewrites iTerm2 session titles so arm mode and attention state are
glanceable on the tab bar itself ("✦[BLOCKED] api-server"), without opening
the TUI. Three render styles, selected via a new, small INI config file at
`~/.relay/config` - which also becomes the durable home for a handful of
existing preferences (sounds, staleness/notify tunables).

Origin: docs/IDEAS.md idea 2. The companion idea (tab-side mode switching)
stays in the backlog; nothing here blocks it.

## 1. Config file

`~/.relay/config`, INI format, parsed with stdlib `configparser`. Path
override for tests: `RELAY_CONFIG`. Read once at startup by both the TUI/
watcher process and CLI verbs that need it (currently none do). A missing
file, missing section, or missing key silently yields defaults. A malformed
file logs one warning line and yields defaults (never crashes the TUI).

Full surface:

```ini
[titles]
style = off            ; off | glyphs | words | hybrid   (default off)

[sounds]
alert = /System/Library/Sounds/Sosumi.aiff   ; watcher alert sound
done  = /System/Library/Sounds/Glass.aiff    ; reserved for done-chimes

[swarm]
stale_minutes   = 10   ; mirrors RELAY_STALE_MINUTES
notify_cooldown = 30   ; mirrors RELAY_NOTIFY_COOLDOWN
```

**Precedence: defaults < config file < environment variable.** Env always
wins, so existing setups and one-off overrides keep working unchanged.

Deliberately NOT configurable here: `RELAY_DB` / `RELAY_CONFIG` (bootstrap
paths), `RELAY_NO_CAFFEINATE` / `RELAY_NO_REACTOR` / `--dry-run`
(session-scoped), spawn boot delay, danger.sh rules (own home), and custom
title glyph/word vocabularies (the vocabulary doubles as the strip-parser;
configurability doubles the bug surface).

New module `iterm/config.py`: `load(path=None) -> Config` (a small dataclass
with typed fields and the precedence already applied). Pure stdlib, no
iterm2 import, unit-tested against temp files.

## 2. Title rendering

New pure module `iterm/titles.py` (no iterm2 import):

- `render(style, mode, state, stale, bare_name) -> str`
- `strip_prefix(title) -> str`

Inputs: `mode` in off|safe|wild|insane, `state` in relay's existing session
states, `stale` bool, `bare_name` = the human's actual tab name.

Attention states are `prompting`, `blocked`, and stale. Non-attention
sessions show at most a mode glyph. Manual (off) + non-attention sessions
render as the bare name (no prefix at all).

| situation          | glyphs   | words                    | hybrid            |
| ------------------ | -------- | ------------------------ | ----------------- |
| safe, working      | `◉ api`  | `[SAFE] api`             | `◉ api`           |
| insane, blocked    | `✦⊘ api` | `[INSANE][BLOCKED] api`  | `✦[BLOCKED] api`  |
| safe, prompting    | `◉‼ api` | `[SAFE][AWAITING] api`   | `◉[AWAITING] api` |
| armed, stale       | `◉? api` | `[SAFE][STALE] api`      | `◉[STALE] api`    |
| manual, blocked    | `⊘ api`  | `[BLOCKED] api`          | `[BLOCKED] api`   |
| manual, idle       | `api`    | `api`                    | `api`             |

Vocabulary (fixed): mode glyphs `◉` safe, `▲` wild, `✦` insane (same as the
TUI's MODE_STYLE); state glyphs `‼` prompting, `⊘` blocked, `?` stale (`?`
because `▲` already means wild). Words: SAFE/WILD/INSANE and
AWAITING/BLOCKED/STALE. When both mode and state render as words they
concatenate without a space (`[INSANE][BLOCKED]`); a glyph cluster is
followed by one space before the name.

`strip_prefix` removes at most one leading relay prefix: a leading cluster
of known glyphs, and/or leading known bracket words, plus the following
space. Unknown bracket text is preserved - a user's genuine `[WIP] foo`
title survives untouched. `strip_prefix(render(...)) == bare_name` for every
style and input combination (round-trip property, tested).

## 3. Watcher integration

- **Read path:** `_session_label` applies `strip_prefix` before returning,
  so the UNIT column, swarm registry, and message addressing always see
  clean names - including prefixes left over from a crashed prior run.
- **Write path:** each poll tick, for every session that is armed OR in an
  attention state, compute desired = `render(style, ...)` over the stripped
  bare name; call `async_set_name` only when desired differs from the
  current on-screen title. Manual + non-attention sessions: if relay
  previously wrote a prefix there (tracked in an in-memory set of session
  ids), write back the bare name once, then leave alone.
- **Quit:** during teardown, restore the bare name on every session relay
  wrote to this run. Best-effort (sessions may already be closed).
- **style = off:** the write path is entirely inert (still strips on read).
- Title writes are best-effort: an iTerm2 error is logged once per session
  and never breaks the poll loop.

`relay spawn` keeps writing bare names; the watcher decorates them on its
next tick if the style calls for it.

## 4. Crash honesty

If relay dies without restoring, prefixes linger on the tab bar until relay
next runs (its write path self-heals: strip-on-read + rewrite/restore) or
the user renames the tab. Same residue class as any title-writing tool.
Documented in the README section.

## 5. Testing

- `iterm/test_titles.py`: render table above verbatim as cases; strip/render
  round-trip for all styles; user `[WIP]`-style titles preserved; double
  prefix never produced (render over an already-prefixed name that was
  stripped first).
- `iterm/test_config.py`: temp-file configs - missing file, partial
  sections, malformed file, env-beats-config precedence for each mirrored
  key.
- Watcher wiring: fake session records `async_set_name` calls (same pattern
  as the `_deliver` tests) - write-only-on-change, no write for manual+idle,
  restore-on-stop, restore-once after disarm.

## 6. Out of scope

Tab colors as a fourth style (parked in docs/IDEAS.md), per-tab style
overrides, prefixing unwatched windows, configurable vocabularies, moving
any other env var into the config file.
