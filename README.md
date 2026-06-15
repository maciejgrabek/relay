# ⟿ Relay

**A control panel for unattended Claude Code sessions.**

Relay lets you run many Claude Code sessions in parallel and *walk away*. You
**arm** the sessions you trust; Relay auto-approves their routine, safe commands
so you stop pretending to be a monkey hitting `Enter`. When a session hits
something genuinely risky - or finishes - Relay **plays a sound** so you know
which terminal needs a human. A small TUI lists every session and lets you
arm/disarm them with the arrow keys.

```
  ██████╗ ███████╗██╗      █████╗ ██╗   ██╗
  ██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝
  ██████╔╝█████╗  ██║     ███████║ ╚████╔╝
  ██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝
  ██║  ██║███████╗███████╗██║  ██║   ██║
  ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝
  RELAY · SESSION CONTROL · 3 units · 2 armed · 7✓ 0⊘
  CORE TEMP ▰▰▰▱▱▱▱▱▱▱  ◷ WARM

  MODE      STATUS      LOC   UNIT            ✓/⊘   LAST DIRECTIVE
▸ ◉ SAFE    ▸ ACTIVE    0.1   api-server      5/0   grep -rn "TODO" src/
  ◉ SAFE    ⊘ LOCKED    0.2   infra-migrate   2/1   terraform apply -auto-…
  ○ MANUAL  ◌ STANDBY   1.0   docs-site       -     -

  ↑↓ move · SPACE arm · ENTER send Enter · 1/2/3 send digit · n go to tab · q quit
```

> **Two implementations live in this repo.** Start with **`relay-iterm`**
> (recommended); the original Claude Code hooks version, **`relay`**, is kept as
> a [legacy track](#legacy-hooks-based-relay). Both share the same safety
> classifier, [`lib/danger.sh`](lib/danger.sh).
>
> - **`relay-iterm`** - iTerm2-native. One Python process, no Claude Code hooks,
>   no session restart. It watches iTerm2 screens and auto-clears safe permission
>   prompts - including Claude Code's obfuscation-detector prompts that hooks
>   *cannot* suppress - by sending `Enter`; it pings you on dangerous ones.
> - **`relay`** - the original Claude Code hooks approach. Requires a session
>   restart to load hooks and cannot touch the obfuscation-detector prompts.

## Why

Claude Code gates many actions behind a `Yes / No` permission prompt. That is a
good safety default, but when you run several long sessions it turns into
constant babysitting - and almost every prompt is for something obviously safe
(`grep`, `cat`, reading files, in-repo edits). Relay automates the safe 90% and
escalates - audibly - only the parts that actually need your judgement.

## How it works

`relay-iterm` talks to **iTerm2's Python API**: one process watches every iTerm2
session's screen, and for the sessions you **arm**, it auto-clears safe
permission prompts by sending `Enter`. It pings you (notification + sound) on
dangerous commands, real questions, and anything it can't classify. No daemon,
no auto-launch, no shared state files - tool on === TUI open; quit === everything
stops.

- **Notify is global.** Any prompt on any tab (armed or not) plays a sound and
  posts a macOS notification - the safe, high-value walk-away half, zero blast
  radius.
- **Inject is narrow.** Only in sessions you've armed, and only when both gates
  below pass.
- While the TUI is open it runs **`caffeinate`** so your Mac (and the armed
  sessions) don't sleep. Quitting releases it. Opt out with `RELAY_NO_CAFFEINATE=1`.

**Why this exists:** Claude Code's built-in command-shape / obfuscation detector
fires permission prompts that **hooks cannot suppress** (they trigger even on
allowlisted commands). Because `relay-iterm` acts at the terminal layer, it
*can* clear those - it's the only one of the two implementations that handles
that whole class.

### Arm levels (per tab)

`Space` cycles each session through these levels:

- **off** (`○ MANUAL`) - manual. Relay watches and shows state, never acts.
- **safe** (`◉ SAFE`) - the two gates below. Approves prompts whose command
  classifies safe; escalates dangerous or unreadable ones to you.
- **wild** (`▲ WILD`) - approves **any** genuine `Do you want to proceed?` prompt
  (cursor on Yes) without classifying the command. Heredocs and obfuscation-
  detector prompts `safe` can't read get cleared.
- **insane** (`✦ INSANE`) - approves **any** tool-permission prompt at all, even
  the fail-safe cases (cursor not on option 1, unparseable command).

**A real question (multi-choice, no proceed-marker) is ALWAYS handed off to
you - NO mode auto-answers your decisions.**

Use `safe` where a wrong Enter would hurt; `wild`/`insane` in scratch/throwaway
workspaces where you just want the friction gone.

### The two gates (safe mode)

For each session armed **safe**, on every screen update:

1. **Type gate** - is the screen a tool-permission prompt (`Do you want to
   proceed?` + a `1. Yes / 2. No` menu)? A *real question* (multi-choice,
   asking for judgement) is left alone - it just notifies you. You stay in
   control of decisions; only routine proceed-prompts are automated.
2. **Safety gate** - for permission prompts only, it reads the command off the
   screen and runs [`lib/danger.sh`](lib/danger.sh). **Safe** -> send `Enter`.
   **Dangerous** -> notify and hand off.

**Fail-safe:** if the command can't be parsed (e.g. a heredoc whose header
scrolled off-screen), the menu's default isn't an affirmative `Yes`, or anything
is ambiguous, `safe` mode **notifies and never injects** - that's when you reach
for `wild`/`insane` on that tab. Alerts and auto-approval happen **only for armed
sessions**; an un-armed tab is display-only.

### The safety boundary (read this)

Relay's gate decides on **what command runs**, not on what a prompt looks like.
The classification lives in [`lib/danger.sh`](lib/danger.sh) - one regex of
read-only leading commands (always allowed) and one of dangerous patterns
(always escalated). **Tune it to your own risk tolerance before trusting it.**

Two limits you must accept:

1. **Default-allow for armed sessions.** In `safe` mode Relay auto-approves any
   command not matched as dangerous, including novel ones. A destructive command
   phrased to dodge the regex would be auto-approved. This is the deliberate
   trade for killing the busywork - only arm sessions in workspaces where that's
   OK.
2. **A dangerous action launched through a safe "leader" slips past.** The simple
   model does not inspect what a script/wrapper does, so `make deploy-prod`,
   `npm run deploy`, or `python3 evil.py` classify SAFE. These "Track 2" gaps are
   tracked as warnings in [`test/danger_test.sh`](test/danger_test.sh) so a
   future change that closes one nags you to promote it. Use manual mode for tabs
   where that matters.

## Requirements

- macOS (uses `afplay`, `caffeinate`, and `osascript` notifications)
- iTerm2 with the **Python API enabled** (Settings -> General -> Magic -> Enable
  Python API)
- Python 3 with the `iterm2` and `textual` modules

## Install and run

```bash
git clone <repo-url> relay
cd relay

pip install iterm2 textual          # one-time deps
# iTerm2: Settings -> General -> Magic -> Enable Python API (once)

# Put the launcher on your PATH:
echo 'export PATH="'"$PWD"'/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

bin/relay-iterm --dry-run           # SAFE FIRST RUN: watch + log, never inject
bin/relay-iterm                     # for real
```

**Start with `--dry-run`.** It classifies real prompts and logs what it *would*
do without sending a single keystroke - the honest way to confirm the prompt
parsing matches your setup before you trust it to press Enter. When the log shows
it correctly tagging your safe prompts INJECT and dangerous ones NOTIFY, drop the
flag. (A typo'd flag is rejected rather than run live, so a mistyped `--dry-run`
can't silently auto-approve.)

### Keys

| Key | Action |
| --- | ------ |
| `↑` `↓` / `j` `k` | Move the cursor |
| `Enter` | **Send Enter** to the selected session (answer its prompt by hand) |
| `1` `2` `3` | **Send that digit** to the selected session |
| `Space` | Cycle arm: off -> `◉` safe -> `▲` wild -> `✦` insane -> off |
| `a` / `d` | Arm all (safe) / disarm all |
| `n` | Go to (focus) the real iTerm2 tab for the selected session |
| `x` | Hide / show the selected session |
| `q` | Quit (tears down the iTerm2 connection, releases `caffeinate`) |

Hidden sessions don't vanish - they drop to a dimmed section at the bottom of the
list, and the cursor skips the divider as you navigate, so you fly between your
kept sessions while still being able to see and un-hide (`x` again) the rest. The
right-hand **preview** pulls the selected session's current screen the moment you
land on it (and updates as that session prints), so you see the live prompt
before you answer it.

**Manual send vs arming are different things.** *Arming* (`Space`) lets Relay
auto-clear *safe* prompts for you. *Manual send* (`Enter` / `1` / `2` / `3`) is
you answering a prompt yourself from the panel - it works on **any** session,
armed or not, **even in `--dry-run`**, because pressing a key is a deliberate
human action, not automatic injection. So you can keep every session in manual
mode and just use Relay as one place to navigate between them and answer.

### Audit trail

Walking away means things get approved while you're not looking - so Relay keeps
a record. The **`✓/⊘` column** shows a per-session running tally: auto-approvals
(green) / escalations (red). The preview spells it out (`CLEARED:N  HELD:M`).

Every auto-approval and escalation is also appended to **`~/.relay/audit.jsonl`**
- one JSON line each: `ts`, `verdict` (`auto-approved` / `escalated` /
`would-approve`), `session`, `command`, `reason`. Manual keypresses are *not*
logged (they're your deliberate actions, not unattended ones). The audit write
happens **before** the Enter is sent, and if that durable write fails Relay
refuses to inject - so an unattended approval can never happen un-recorded.
Entries older than **7 days** are pruned once each time you launch the TUI.
Tunable via `RELAY_AUDIT_LOG` and `RELAY_AUDIT_RETENTION_DAYS`.

```bash
# what got auto-approved in the last day, newest last:
tail -50 ~/.relay/audit.jsonl | jq -r 'select(.verdict=="auto-approved") | "\(.session): \(.command)"'
```

### What's verified, and the one gap

Tested: the gate logic against real captured prompts (incl. the API's NUL/nbsp
cell encoding), live connect/enumerate/stream/teardown, and the TUI render +
controls headless. **Not** yet exercised end-to-end on a live *fired* prompt -
that's precisely what `--dry-run` is for. The `danger.sh` Track-2 leader gaps
(above) apply; see [`test/danger_test.sh`](test/danger_test.sh).

## Configuration

Environment variables (set before launching `relay-iterm`):

| Variable                     | Default                    | Purpose                                   |
| ---------------------------- | -------------------------- | ----------------------------------------- |
| `RELAY_AUDIT_LOG`            | `~/.relay/audit.jsonl`     | Where the audit trail is written          |
| `RELAY_AUDIT_RETENTION_DAYS` | `7`                        | Days of audit history kept at launch      |
| `RELAY_NOTIFY_COOLDOWN`      | `30`                       | Min seconds between alerts per session    |
| `RELAY_NO_CAFFEINATE`        | unset                      | Set to `1` to not keep the Mac awake      |
| `RELAY_NO_REACTOR`           | unset                      | Set to `1` to hide the reactor meter      |

Risk posture (which commands auto-approve vs escalate) is edited directly in
[`lib/danger.sh`](lib/danger.sh).

## Project layout

```
relay/
  bin/relay-iterm        # launcher (the recommended tool)
  iterm/app.py           # Textual TUI (the control panel)
  iterm/watcher.py       # iTerm2 connection: stream screens, run gates, inject
  iterm/gates.py         # pure gate logic (type + safety), no iTerm2 imports
  iterm/audit.py         # durable audit log of unattended decisions
  iterm/test_*.py        # gate/TUI suites, built from real captured prompts
  lib/danger.sh          # shared command-classification rules (tune me)
  test/danger_test.sh    # classifier regression suite (run before tuning danger.sh)
  test/run.sh            # run the whole suite (bash + Python), no pytest needed

  bin/relay              # LEGACY: the hooks-based TUI (see below)
  hooks/relay-gate.sh    # LEGACY: PreToolUse classify + arm-aware auto-approve
  hooks/relay-status.sh  # LEGACY: writes session state for the legacy TUI
  install.sh uninstall.sh# LEGACY: safe, idempotent settings merge
```

## Tests

No pytest needed - each Python suite has a `__main__` runner.

```bash
./test/run.sh                # bash classifier + all Python gate/TUI suites
./test/danger_test.sh -v     # just the classifier, verbose (lists every case)
```

Run them after editing `lib/danger.sh`. The classifier suite also tracks the
known Track-2 "command-shape" gaps as warnings, so you'll know when a future
change closes one.

## Legacy: hooks-based Relay

The original implementation, kept for reference. It uses **Claude Code hooks + a
TUI over a shared state dir** instead of the iTerm2 API. It cannot suppress
Claude Code's obfuscation-detector prompts, and it requires restarting sessions
to load the hooks - which is why `relay-iterm` superseded it. Prefer `relay-iterm`
unless you specifically want a hooks-based approach.

- A **`PreToolUse` hook** (`relay-gate.sh`) classifies each Bash command. If the
  session is *armed* and the command is safe, it auto-approves; if dangerous (see
  `lib/danger.sh`), it forces the normal prompt and rings the alert sound; if the
  session is *disarmed*, the hook does nothing and you get stock Claude Code.
- **`Stop` / `Notification` / `UserPromptSubmit` hooks** record each session's
  state into `~/.relay/sessions/<id>.json` and play the done/alert sounds.
- **`relay`** (the TUI) reads that dir and lets you arm/disarm sessions by
  creating/removing `<id>.armed` flag files, which the gate hook checks. It runs
  `caffeinate` while open.

### Install / uninstall (legacy)

Requires `jq`.

```bash
# Per-project (writes ./.claude/settings.local.json in the CURRENT project):
cd /path/to/your/project && /path/to/relay/install.sh

# Or globally for every project:
/path/to/relay/install.sh --global
```

Then **restart any open Claude Code sessions** (hooks load at startup) and run
`relay`. The installer is idempotent and writes a timestamped backup of your
settings file before any change. To remove: `./uninstall.sh` (same `--global` /
`--target` flags).

### Usage (legacy)

| Key            | Action                              |
| -------------- | ----------------------------------- |
| `↑` / `↓` `j`/`k` | Move the cursor                  |
| `Enter` / `Space` | Connect / disconnect the session |
| `a`            | Connect all                         |
| `d`            | Disconnect all                      |
| `q`            | Quit (releases `caffeinate`)        |

Closed tabs leave state files behind in `~/.relay/sessions/`. The TUI greys them
as `(stale)`; run `relay --prune` to delete orphan arm-flags and sessions
untouched for longer than `RELAY_PRUNE_AGE` (default `86400`, one day).

### Configuration (legacy)

| Variable               | Default                                | Purpose                          |
| ---------------------- | -------------------------------------- | -------------------------------- |
| `RELAY_HOME`           | `~/.relay`                             | Where session state is stored    |
| `RELAY_ALERT_SOUND`    | `/System/Library/Sounds/Sosumi.aiff`   | "Needs you" sound                |
| `RELAY_DONE_SOUND`     | `/System/Library/Sounds/Glass.aiff`    | "Session done" sound             |
| `RELAY_STALE`          | `180`                                  | Seconds before a session is grey |
| `RELAY_PRUNE_AGE`      | `86400`                                | Age (s) for `--prune` to remove  |
| `RELAY_NO_CAFFEINATE`  | unset                                  | Set to `1` to not keep Mac awake |

## License

MIT - see [LICENSE](LICENSE).
