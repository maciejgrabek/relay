# Tab-Title Status Prefixes + Config File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relay rewrites iTerm2 session titles ("✦[BLOCKED] api-server") so mode + attention state are glanceable on the tab bar, configured via a new `~/.relay/config` INI file that also becomes home for sounds and swarm tunables.

**Architecture:** Two new pure modules - `iterm/config.py` (INI loader with defaults < config < env precedence) and `iterm/titles.py` (render + strip_prefix, the strip being the crash-safety mechanism). The watcher gains a title write path in its existing poll loop (write only on change, restore on quit, never in dry-run) and strips prefixes on read so the UNIT column and swarm registry always see bare names. Spec: `docs/specs/2026-07-15-tab-title-prefixes-design.md`.

**Tech Stack:** Python 3 stdlib (`configparser`, `dataclasses`, `re`). No new dependencies.

## Global Constraints

- NO em-dash characters (U+2014) anywhere. Plain `-` only. The glyphs `◉ ▲ ✦ ‼ ⊘` and box-drawing characters in code ARE required - copy exactly.
- No pytest: test files are `iterm/test_*.py` with `run()`/`__main__` runners (exit 0/1), auto-globbed by `test/run.sh`.
- `config.py` and `titles.py` import neither `iterm2` nor `sqlite3`.
- Config precedence exactly: defaults < config file < environment variable. Env keys mirrored: `RELAY_STALE_MINUTES`, `RELAY_NOTIFY_COOLDOWN`. `title_style` and sounds have NO env mirror.
- `title_style` values exactly: `off | glyphs | words | hybrid`, default `off`. Invalid value -> warning + `off`.
- Title vocabulary (fixed): mode glyphs `◉` safe / `▲` wild / `✦` insane; state glyphs `‼` prompting / `⊘` blocked / `⧗` stale; words SAFE/WILD/INSANE and AWAITING/BLOCKED/STALE. State priority: blocked > prompting > stale (one state indicator max).
- Titles are never written in `--dry-run`.
- Commit after every task; short imperative subjects; no Co-Authored-By trailer.

## Reference: codebase facts

- `iterm/watcher.py`: `Watcher.__init__(connection, alert_sound="/System/Library/Sounds/Sosumi.aiff", done_sound="/System/Library/Sounds/Glass.aiff", on_change=None, dry_run=False)` currently reads `RELAY_NOTIFY_COOLDOWN` (line ~116) and `RELAY_STALE_MINUTES` (line ~121) directly from env. `start()` runs the poll loop; per-session block calls `_snapshot`/`_handle`/`_deliver` (inside `if res:`) then `_check_stale(info)`; `finally:` calls `await self._close_connection()`. `_sync_sessions` sets `info.title = await self._session_label(s, tab)`. `SessionInfo` is a dataclass with `stale: bool` and private fields via `field(default=..., repr=False)`.
- `iterm/app.py` constructs `Watcher(connection, on_change=..., dry_run=...)` - no sound args - so changing sound defaults to config-resolved values needs no app change.
- `iterm/test_watcher.py`: `FakeSession` records `async_send_text` into `self.sent`; tests monkeypatch `W.notify_mac`, `W.audit.record`, `W.swarmdb.*`; async test funcs (`go()`, `deliver_tests()`) run via `asyncio.run` in `__main__`, each with a local `chk(name, cond)`.
- Test style everywhere: `check(msg, cond)` prints OK/FAIL, `run() -> bool`, `sys.exit(0 if run() else 1)`.

## File structure

```
iterm/config.py        # Config dataclass + load(): INI + env precedence
iterm/titles.py        # render(style, mode, state, stale, bare) / strip_prefix(title)
iterm/test_config.py   # temp-file INI tests
iterm/test_titles.py   # render table + round-trip property tests
iterm/watcher.py       # MODIFY: cfg wiring, raw-title tracking + strip-on-read,
                       #         _apply_title / _restore_titles in the poll loop
iterm/test_watcher.py  # MODIFY: title_tests() + hermetic RELAY_CONFIG
README.md              # MODIFY: config file + titles section
```

---

### Task 1: config.py - INI loader with precedence

**Files:**
- Create: `iterm/config.py`
- Test: `iterm/test_config.py`

**Interfaces:**
- Produces: `config.Config` frozen dataclass: `title_style: str = "off"`, `alert_sound: str = "/System/Library/Sounds/Sosumi.aiff"`, `done_sound: str = "/System/Library/Sounds/Glass.aiff"`, `stale_minutes: float = 10.0`, `notify_cooldown: float = 30.0`
- Produces: `config.load(path=None) -> tuple[Config, list[str]]` - warnings list is human-readable strings; never raises. `path=None` -> `$RELAY_CONFIG` -> `~/.relay/config`.

- [ ] **Step 1: Write the failing test**

Create `iterm/test_config.py`:

```python
"""Tests for the ~/.relay/config INI loader. Temp files, no iTerm2 imports.

Run: python3 iterm/test_config.py    or    ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".ini")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def run():
    ok = True
    # Hermetic: no env leakage between cases.
    for k in ("RELAY_CONFIG", "RELAY_STALE_MINUTES", "RELAY_NOTIFY_COOLDOWN"):
        os.environ.pop(k, None)

    # Missing file -> pure defaults, no warnings.
    cfg, warns = config.load("/nonexistent/relay-config")
    ok &= check("missing file -> defaults", cfg.title_style == "off"
                and cfg.stale_minutes == 10.0 and cfg.notify_cooldown == 30.0
                and cfg.alert_sound.endswith("Sosumi.aiff")
                and cfg.done_sound.endswith("Glass.aiff"))
    ok &= check("missing file -> no warnings", warns == [])

    # Full file -> every key read.
    p = _write("[titles]\nstyle = hybrid\n"
               "[sounds]\nalert = /tmp/a.aiff\ndone = /tmp/d.aiff\n"
               "[swarm]\nstale_minutes = 5\nnotify_cooldown = 60\n")
    cfg, warns = config.load(p)
    ok &= check("full file read", cfg.title_style == "hybrid"
                and cfg.alert_sound == "/tmp/a.aiff"
                and cfg.done_sound == "/tmp/d.aiff"
                and cfg.stale_minutes == 5.0 and cfg.notify_cooldown == 60.0)
    ok &= check("full file -> no warnings", warns == [])

    # Partial file -> missing keys keep defaults.
    p = _write("[titles]\nstyle = glyphs\n")
    cfg, _ = config.load(p)
    ok &= check("partial file keeps defaults", cfg.title_style == "glyphs"
                and cfg.stale_minutes == 10.0)

    # Invalid style -> warning + off.
    p = _write("[titles]\nstyle = neon\n")
    cfg, warns = config.load(p)
    ok &= check("invalid style -> off + warning", cfg.title_style == "off"
                and any("neon" in w for w in warns))

    # Non-numeric tunable -> warning + default.
    p = _write("[swarm]\nstale_minutes = soon\n")
    cfg, warns = config.load(p)
    ok &= check("bad float -> default + warning", cfg.stale_minutes == 10.0
                and any("stale_minutes" in w for w in warns))

    # Malformed INI -> defaults + one warning, never raises.
    p = _write("this is not ini [ at all\n= = =\n")
    cfg, warns = config.load(p)
    ok &= check("malformed file -> defaults + warning",
                cfg.title_style == "off" and len(warns) >= 1)

    # Env beats config for the two mirrored keys.
    p = _write("[swarm]\nstale_minutes = 5\nnotify_cooldown = 60\n")
    os.environ["RELAY_STALE_MINUTES"] = "2"
    os.environ["RELAY_NOTIFY_COOLDOWN"] = "7"
    try:
        cfg, _ = config.load(p)
        ok &= check("env beats config", cfg.stale_minutes == 2.0
                    and cfg.notify_cooldown == 7.0)
    finally:
        os.environ.pop("RELAY_STALE_MINUTES", None)
        os.environ.pop("RELAY_NOTIFY_COOLDOWN", None)

    # RELAY_CONFIG env selects the path when load() gets None.
    p = _write("[titles]\nstyle = words\n")
    os.environ["RELAY_CONFIG"] = p
    try:
        cfg, _ = config.load()
        ok &= check("RELAY_CONFIG path honored", cfg.title_style == "words")
    finally:
        os.environ.pop("RELAY_CONFIG", None)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_config.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: Write the implementation**

Create `iterm/config.py`:

```python
"""Relay config - ~/.relay/config (INI), the durable home for preferences.

    [titles]
    style = off            ; off | glyphs | words | hybrid

    [sounds]
    alert = /System/Library/Sounds/Sosumi.aiff
    done  = /System/Library/Sounds/Glass.aiff

    [swarm]
    stale_minutes   = 10   ; mirrors RELAY_STALE_MINUTES
    notify_cooldown = 30   ; mirrors RELAY_NOTIFY_COOLDOWN

Precedence: defaults < config file < environment variable. Env always wins,
so existing setups keep working. A missing file, section, or key silently
yields defaults; a malformed file or value yields defaults plus a warning
string (returned, not printed - the caller decides where warnings go).
Session-scoped things (dry-run, RELAY_NO_CAFFEINATE, RELAY_DB) deliberately
stay out of this file.

Pure stdlib, no iterm2/sqlite imports (test_config.py runs it standalone).
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

TITLE_STYLES = ("off", "glyphs", "words", "hybrid")


@dataclass(frozen=True)
class Config:
    title_style: str = "off"
    alert_sound: str = "/System/Library/Sounds/Sosumi.aiff"
    done_sound: str = "/System/Library/Sounds/Glass.aiff"
    stale_minutes: float = 10.0
    notify_cooldown: float = 30.0


def default_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_CONFIG", "~/.relay/config"))


def _get_float(cp, section, key, fallback, warns) -> float:
    raw = cp.get(section, key, fallback=None)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError:
        warns.append(f"config: [{section}] {key} = {raw!r} is not a number - "
                     f"using {fallback}")
        return fallback


def load(path: Optional[str] = None) -> Tuple[Config, List[str]]:
    """Read the config file and apply env overrides. Never raises."""
    p = path or default_path()
    warns: List[str] = []
    cp = configparser.ConfigParser()
    try:
        cp.read(p)
    except configparser.Error as e:
        warns.append(f"config: {p} is malformed ({e.__class__.__name__}) - "
                     f"using defaults")
        cp = configparser.ConfigParser()

    d = Config()  # defaults
    style = cp.get("titles", "style", fallback=d.title_style).strip().lower()
    if style not in TITLE_STYLES:
        warns.append(f"config: [titles] style = {style!r} is not one of "
                     f"{'/'.join(TITLE_STYLES)} - using 'off'")
        style = "off"

    stale = _get_float(cp, "swarm", "stale_minutes", d.stale_minutes, warns)
    cooldown = _get_float(cp, "swarm", "notify_cooldown", d.notify_cooldown,
                          warns)

    # Env wins over the file for the two mirrored keys.
    env_stale = os.environ.get("RELAY_STALE_MINUTES")
    if env_stale is not None:
        try:
            stale = float(env_stale)
        except ValueError:
            warns.append(f"config: RELAY_STALE_MINUTES = {env_stale!r} is "
                         f"not a number - ignored")
    env_cool = os.environ.get("RELAY_NOTIFY_COOLDOWN")
    if env_cool is not None:
        try:
            cooldown = float(env_cool)
        except ValueError:
            warns.append(f"config: RELAY_NOTIFY_COOLDOWN = {env_cool!r} is "
                         f"not a number - ignored")

    return Config(
        title_style=style,
        alert_sound=cp.get("sounds", "alert", fallback=d.alert_sound).strip(),
        done_sound=cp.get("sounds", "done", fallback=d.done_sound).strip(),
        stale_minutes=stale,
        notify_cooldown=cooldown,
    ), warns
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_config.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 5: Commit**

```bash
git add iterm/config.py iterm/test_config.py
git commit -m "config: INI loader for ~/.relay/config with env-wins precedence"
```

---

### Task 2: titles.py - render and strip

**Files:**
- Create: `iterm/titles.py`
- Test: `iterm/test_titles.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces: `titles.render(style, mode, state, stale, bare) -> str` where style in `off|glyphs|words|hybrid`, mode in `off|safe|wild|insane`, state is a watcher session state string, stale is bool.
- Produces: `titles.strip_prefix(title) -> str` - removes at most one leading relay prefix; unknown bracket text (e.g. a user's `[WIP] foo`) is preserved.
- Invariant later tasks rely on: `strip_prefix(render(style, m, s, st, bare)) == bare` for every combination, and `render(..., bare)` never double-prefixes because callers pass an already-stripped bare name.

- [ ] **Step 1: Write the failing test**

Create `iterm/test_titles.py`:

```python
"""Tests for tab-title prefix rendering/stripping (pure logic).

Run: python3 iterm/test_titles.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from titles import render, strip_prefix  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    N = "api-server"

    # --- the spec's render table, verbatim -----------------------------------
    ok &= check("glyphs: safe working", render("glyphs", "safe", "working", False, N) == f"◉ {N}")
    ok &= check("words: safe working", render("words", "safe", "working", False, N) == f"[SAFE] {N}")
    ok &= check("hybrid: safe working", render("hybrid", "safe", "working", False, N) == f"◉ {N}")

    ok &= check("glyphs: insane blocked", render("glyphs", "insane", "blocked", False, N) == f"✦⊘ {N}")
    ok &= check("words: insane blocked", render("words", "insane", "blocked", False, N) == f"[INSANE][BLOCKED] {N}")
    ok &= check("hybrid: insane blocked", render("hybrid", "insane", "blocked", False, N) == f"✦[BLOCKED] {N}")

    ok &= check("glyphs: safe prompting", render("glyphs", "safe", "prompting", False, N) == f"◉‼ {N}")
    ok &= check("words: safe prompting", render("words", "safe", "prompting", False, N) == f"[SAFE][AWAITING] {N}")
    ok &= check("hybrid: safe prompting", render("hybrid", "safe", "prompting", False, N) == f"◉[AWAITING] {N}")

    ok &= check("glyphs: safe stale", render("glyphs", "safe", "idle", True, N) == f"◉⧗ {N}")
    ok &= check("words: safe stale", render("words", "safe", "idle", True, N) == f"[SAFE][STALE] {N}")
    ok &= check("hybrid: safe stale", render("hybrid", "safe", "idle", True, N) == f"◉[STALE] {N}")

    ok &= check("glyphs: manual blocked", render("glyphs", "off", "blocked", False, N) == f"⊘ {N}")
    ok &= check("words: manual blocked", render("words", "off", "blocked", False, N) == f"[BLOCKED] {N}")
    ok &= check("hybrid: manual blocked", render("hybrid", "off", "blocked", False, N) == f"[BLOCKED] {N}")

    for style in ("glyphs", "words", "hybrid"):
        ok &= check(f"{style}: manual idle untouched",
                    render(style, "off", "idle", False, N) == N)
    ok &= check("off style: always bare",
                render("off", "insane", "blocked", True, N) == N)

    # State priority: blocked > prompting > stale (stale + blocked -> blocked).
    ok &= check("priority: blocked beats stale",
                render("hybrid", "safe", "blocked", True, N) == f"◉[BLOCKED] {N}")

    # wild mode glyph
    ok &= check("wild glyph", render("glyphs", "wild", "working", False, N) == f"▲ {N}")

    # --- strip_prefix ---------------------------------------------------------
    ok &= check("strip glyph cluster", strip_prefix(f"✦⊘ {N}") == N)
    ok &= check("strip word pair", strip_prefix(f"[INSANE][BLOCKED] {N}") == N)
    ok &= check("strip hybrid", strip_prefix(f"◉[AWAITING] {N}") == N)
    ok &= check("strip mode-only", strip_prefix(f"▲ {N}") == N)
    ok &= check("bare name untouched", strip_prefix(N) == N)
    ok &= check("user [WIP] title preserved", strip_prefix("[WIP] foo") == "[WIP] foo")
    ok &= check("empty title", strip_prefix("") == "")
    ok &= check("prefix-like glyph inside name kept",
                strip_prefix("api ◉ server") == "api ◉ server")
    ok &= check("user '? help' title preserved", strip_prefix("? help") == "? help")
    ok &= check("stale glyph round-trip",
                strip_prefix(render("glyphs", "off", "idle", True, "api")) == "api")

    # --- round-trip property over the full input space ------------------------
    rt = True
    for style in ("glyphs", "words", "hybrid"):
        for mode in ("off", "safe", "wild", "insane"):
            for state in ("idle", "working", "prompting", "blocked", "cleared"):
                for stale in (False, True):
                    t = render(style, mode, state, stale, N)
                    if strip_prefix(t) != N:
                        print(f"  round-trip FAIL: {style}/{mode}/{state}/{stale} -> {t!r}")
                        rt = False
    ok &= check("round-trip: strip(render(...)) == bare for all combos", rt)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 iterm/test_titles.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'titles'`

- [ ] **Step 3: Write the implementation**

Create `iterm/titles.py`:

```python
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
# stale uses "⧗" - a glyph nobody types in a real tab title, so strip_prefix
# can never eat a user's name.
STATE_GLYPH = {"blocked": "⊘", "prompting": "‼", "stale": "⧗"}
STATE_WORD = {"blocked": "BLOCKED", "prompting": "AWAITING", "stale": "STALE"}

# Strip exactly one leading relay prefix: an optional mode glyph, an optional
# state glyph, then up to two known bracket words, then the separating space.
# Unknown bracket words ([WIP]) don't match, so user titles survive.
_PREFIX_RE = re.compile(
    r"^[◉▲✦]?[‼⊘⧗]?"
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
```

- [ ] **Step 4: Run tests**

Run: `python3 iterm/test_titles.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 5: Commit**

```bash
git add iterm/titles.py iterm/test_titles.py
git commit -m "titles: pure render/strip logic for tab-name status prefixes"
```

---

### Task 3: Watcher - config wiring + strip-on-read

**Files:**
- Modify: `iterm/watcher.py`
- Modify: `iterm/test_watcher.py` (hermetic config only)

**Interfaces:**
- Consumes: `config.load() -> (Config, warnings)`, `titles.strip_prefix`.
- Produces: `Watcher.__init__(connection, alert_sound=None, done_sound=None, on_change=None, dry_run=False, cfg=None)` - sounds default to config values; `self.cfg` is a `config.Config`; `self.notify_cooldown` and `self.stale_after` come from cfg (which already applied env-wins). `SessionInfo._raw_title` holds the UNSTRIPPED on-screen title; `info.title` is always stripped.
- Behavior later tasks rely on: config warnings surface once via `self._note` at the top of `start()`.

- [ ] **Step 1: Make the existing watcher tests hermetic**

In `iterm/test_watcher.py`, after `import os` / before `import watcher as W`, add:

```python
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"
```

- [ ] **Step 2: Modify `iterm/watcher.py` imports and `__init__`**

Add to the imports (after `import swarm`):

```python
import config as relay_config
import titles
```

Replace the `__init__` signature and the affected lines:

```python
    def __init__(self, connection,
                 alert_sound=None,
                 done_sound=None,
                 on_change: Optional[Callable[[], None]] = None,
                 dry_run: bool = False,
                 cfg=None):
        self.connection = connection
        # Config: defaults < ~/.relay/config < env (load() applies all three).
        if cfg is None:
            cfg, cfg_warnings = relay_config.load()
        else:
            cfg_warnings = []
        self.cfg = cfg
        self._cfg_warnings = cfg_warnings
        self.alert_sound = alert_sound or cfg.alert_sound
        self.done_sound = done_sound or cfg.done_sound
```

and replace the two direct env reads:

```python
        self.notify_cooldown = float(os.environ.get("RELAY_NOTIFY_COOLDOWN", "30"))
```
becomes
```python
        self.notify_cooldown = cfg.notify_cooldown
```
and
```python
        self.stale_after = float(
            os.environ.get("RELAY_STALE_MINUTES", "10")) * 60.0
```
becomes
```python
        self.stale_after = cfg.stale_minutes * 60.0
```

Also add the title bookkeeping to `__init__` (with the other swarm state):

```python
        # --- tab-title prefixes (style from config; off = fully inert) ---
        self._titled: set = set()          # session ids we wrote a prefix to
        self._title_err_noted: set = set() # sessions with a logged write error
```

- [ ] **Step 3: Surface config warnings once in `start()`**

At the top of `start()`, right after `self._stop_event.clear()`:

```python
        for w in self._cfg_warnings:
            self._note(w)
        self._cfg_warnings = []
```

- [ ] **Step 4: Track raw titles and strip on read**

Add to `SessionInfo` (next to the other private fields):

```python
    _raw_title: str = field(default="", repr=False)  # unstripped on-screen title
```

In `_sync_sessions`, the two places that assign `info.title` from
`self._session_label(...)` change from:

```python
                    title = await self._session_label(s, tab)
```
(keep that line) and then wherever `info.title = title` / `SessionInfo(..., title=title, ...)` occurs, split raw vs stripped:

```python
                    raw_title = title
                    title = titles.strip_prefix(raw_title)
```

For the create branch: `SessionInfo(session_id=sid, title=title, ...)` then
`info._raw_title = raw_title` right after construction. For the update
branch: `info.title = title` and `info._raw_title = raw_title`.

- [ ] **Step 5: Run the suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED` (behavior identical when no config file exists: sounds and tunables resolve to the same defaults, and strip_prefix of an unprefixed title is the identity).

- [ ] **Step 6: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "watcher: resolve sounds and tunables from config; strip title prefixes on read"
```

---

### Task 4: Watcher - title write path + restore on quit

**Files:**
- Modify: `iterm/watcher.py`
- Test: `iterm/test_watcher.py` (new `title_tests()`)

**Interfaces:**
- Consumes: `titles.render`, `SessionInfo._raw_title` (Task 3), `self.cfg.title_style`, `self._titled` / `self._title_err_noted`.
- Produces: `Watcher._apply_title(info)` (async, called per session per tick after `self._check_stale(info)`), `Watcher._restore_titles()` (async, called in `start()`'s `finally` before `_close_connection`). FakeSession in tests gains `async_set_name` recording into `self.names`.

- [ ] **Step 1: Write the failing tests**

In `iterm/test_watcher.py`, extend `FakeSession`:

```python
class FakeSession:
    def __init__(self):
        self.sent = []
        self.names = []

    async def async_send_text(self, t):
        self.sent.append(t)

    async def async_set_name(self, n):
        self.names.append(n)
```

Append after `deliver_tests()`:

```python
async def title_tests():
    """Drive Watcher._apply_title/_restore_titles against fake sessions."""
    from watcher import Watcher, SessionInfo
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    W.notify_mac = lambda *a, **k: None
    cfg = C.Config(title_style="hybrid")

    def _mk(w, sid, bare, mode="off", state="idle"):
        fs = FakeSession()
        info = SessionInfo(sid, title=bare, _iterm_session=fs,
                           mode=mode, state=state)
        info._raw_title = bare
        w.sessions[sid] = info
        return info, fs

    # Armed session gets a prefix written once; unchanged next tick.
    w = Watcher(connection=None, dry_run=False, cfg=cfg)
    info, fs = _mk(w, "s1", "api", mode="safe", state="working")
    await w._apply_title(info)
    chk("write: armed session prefixed once", fs.names == ["◉ api"])
    info._raw_title = "◉ api"            # what iTerm now shows
    await w._apply_title(info)
    chk("write: no rewrite when unchanged", fs.names == ["◉ api"])
    chk("write: session tracked as titled", "s1" in w._titled)

    # State change rewrites; disarm+calm restores the bare name once.
    info.state = "blocked"
    await w._apply_title(info)
    chk("write: state change rewrites", fs.names[-1] == "◉[BLOCKED] api")
    info._raw_title = fs.names[-1]
    info.mode, info.state = "off", "idle"
    await w._apply_title(info)
    chk("restore: disarmed+calm restored bare", fs.names[-1] == "api")
    chk("restore: untracked after restore", "s1" not in w._titled)
    info._raw_title = "api"
    await w._apply_title(info)
    chk("restore: only once", fs.names[-1] == "api" and len(fs.names) == 3)

    # Manual+idle session never touched.
    info2, fs2 = _mk(w, "s2", "notes")
    await w._apply_title(info2)
    chk("manual+idle: never written", fs2.names == [])

    # style=off: fully inert even for armed sessions.
    w_off = Watcher(connection=None, dry_run=False, cfg=C.Config())
    info3, fs3 = _mk(w_off, "s3", "api", mode="insane", state="blocked")
    await w_off._apply_title(info3)
    chk("style off: inert", fs3.names == [])

    # dry-run: no title writes.
    w_dry = Watcher(connection=None, dry_run=True, cfg=cfg)
    info4, fs4 = _mk(w_dry, "s4", "api", mode="safe", state="blocked")
    await w_dry._apply_title(info4)
    chk("dry-run: no writes", fs4.names == [])

    # restore-on-quit restores every titled session.
    w2 = Watcher(connection=None, dry_run=False, cfg=cfg)
    infoa, fsa = _mk(w2, "sa", "alpha", mode="safe", state="working")
    infob, fsb = _mk(w2, "sb", "beta", mode="wild", state="blocked")
    await w2._apply_title(infoa)
    await w2._apply_title(infob)
    await w2._restore_titles()
    chk("quit: all titled sessions restored",
        fsa.names[-1] == "alpha" and fsb.names[-1] == "beta"
        and not w2._titled)

    # a failing async_set_name is logged once and never raises.
    class BoomSession(FakeSession):
        async def async_set_name(self, n):
            raise RuntimeError("boom")
    w3 = Watcher(connection=None, dry_run=False, cfg=cfg)
    fsx = BoomSession()
    infox = SessionInfo("sx", title="x", _iterm_session=fsx,
                        mode="safe", state="working")
    infox._raw_title = "x"
    w3.sessions["sx"] = infox
    await w3._apply_title(infox)
    await w3._apply_title(infox)
    chk("write error: logged once, never raises",
        sum("title write failed" in l for l in w3.log) == 1)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok
```

And extend `__main__`:

```python
if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = asyncio.run(deliver_tests())
    r3 = asyncio.run(title_tests())
    sys.exit(0 if (r1 and r2 and r3) else 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 iterm/test_watcher.py`
Expected: FAIL with `AttributeError: 'Watcher' object has no attribute '_apply_title'`

- [ ] **Step 3: Implement `_apply_title` and `_restore_titles`** (place after `_check_gone`)

```python
    # --- tab-title prefixes -------------------------------------------------

    async def _apply_title(self, info: SessionInfo) -> None:
        """Keep the session's on-screen title in sync with mode + attention
        state. Writes only when the desired title differs from what's on
        screen; restores the bare name once when a previously-prefixed
        session goes manual+calm. Fully inert when style is off or dry-run
        (dry-run mutates nothing, titles included). Best-effort: an iTerm2
        error is logged once per session and never breaks the poll loop."""
        if self.cfg.title_style == "off" or self.dry_run:
            return
        s = info._iterm_session
        if s is None:
            return
        desired = titles.render(self.cfg.title_style, info.mode, info.state,
                                info.stale, info.title)
        if desired == info.title and info.session_id not in self._titled:
            return                       # nothing to add, nothing to restore
        if desired == info._raw_title:
            # Screen already correct; just keep bookkeeping accurate.
            if desired == info.title:
                self._titled.discard(info.session_id)
            else:
                self._titled.add(info.session_id)
            return
        try:
            await s.async_set_name(desired)
            info._raw_title = desired
            if desired == info.title:
                self._titled.discard(info.session_id)   # bare name restored
            else:
                self._titled.add(info.session_id)
        except Exception as e:
            if info.session_id not in self._title_err_noted:
                self._title_err_noted.add(info.session_id)
                self._note(f"title write failed {info.title}: {e}")

    async def _restore_titles(self) -> None:
        """On quit: write the bare name back to every session we prefixed.
        Best-effort - sessions may already be gone."""
        for sid in list(self._titled):
            info = self.sessions.get(sid)
            if info is not None and info._iterm_session is not None:
                try:
                    await info._iterm_session.async_set_name(info.title)
                except Exception:
                    pass
            self._titled.discard(sid)
```

- [ ] **Step 4: Wire into the poll loop and teardown**

In `start()`'s per-session block, right after `self._check_stale(info)`:

```python
                        await self._apply_title(info)
```

In `start()`'s `finally:` block, before `await self._close_connection()`:

```python
            await self._restore_titles()
```

- [ ] **Step 5: Run tests**

Run: `python3 iterm/test_watcher.py` then `./test/run.sh`
Expected: `ALL PASS` / `ALL SUITES PASSED`

- [ ] **Step 6: Commit**

```bash
git add iterm/watcher.py iterm/test_watcher.py
git commit -m "watcher: write mode/state prefixes into tab titles, restore on quit"
```

---

### Task 5: README + sample config + final sweep

**Files:**
- Modify: `README.md`
- Modify: `docs/specs/2026-07-15-tab-title-prefixes-design.md` (status line)

- [ ] **Step 1: README**

Add a `## Configuration file` subsection inside the existing Configuration
section (keep the env-var table; note the precedence rule "defaults < config
file < env var" and that `RELAY_STALE_MINUTES` / `RELAY_NOTIFY_COOLDOWN` now
also have config-file homes). Include the full sample:

```ini
# ~/.relay/config
[titles]
style = hybrid         ; off | glyphs | words | hybrid (default off)

[sounds]
alert = /System/Library/Sounds/Sosumi.aiff
done  = /System/Library/Sounds/Glass.aiff

[swarm]
stale_minutes   = 10
notify_cooldown = 30
```

Add a `### Tab-title prefixes` subsection: the render table from the spec
(all three styles), armed-or-attention write scope, restore-on-quit,
never-in-dry-run, and the crash-honesty paragraph (leftover prefixes are
stripped on read and cleaned up by the next run; a user rename also clears
them). Update the project layout tree with `iterm/config.py`,
`iterm/titles.py`, and the two new test files. Voice: relay's existing README
(direct, safety-honest). NO em-dash.

- [ ] **Step 2: Spec status**

In `docs/specs/2026-07-15-tab-title-prefixes-design.md` change
`**Status:** Approved for planning` to
`**Status:** Implemented (see docs/plans/2026-07-15-tab-title-prefixes.md)`.

- [ ] **Step 3: Final sweep**

```bash
./test/run.sh                              # ALL SUITES PASSED
grep -rn $'\u2014' iterm/ README.md docs/specs/2026-07-15-tab-title-prefixes-design.md || echo "no em-dashes"
python3 - <<'EOF'
import sys; sys.path.insert(0, "iterm")
import config, titles
cfg, w = config.load("/nonexistent")
print(cfg, w)
print(titles.render("hybrid", "insane", "blocked", False, "api"))
EOF
```

Expected: suite green, "no em-dashes", `Config(...) []` and `✦[BLOCKED] api`.

Live checks (HUMAN, deferred - list them in the final report, do not run):
set `style = hybrid` in `~/.relay/config`, run `bin/relay`, arm a tab, watch
its title gain `◉`; trigger a prompt, watch `[AWAITING]`; quit relay, watch
titles restore.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/specs/2026-07-15-tab-title-prefixes-design.md
git commit -m "docs: config file and tab-title prefixes"
```
