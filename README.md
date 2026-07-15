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

> **`relay`** is iTerm2-native: one Python process, no Claude Code hooks,
> no session restart. It watches iTerm2 screens and auto-clears safe permission
> prompts - including Claude Code's obfuscation-detector prompts that hooks
> *cannot* suppress - by sending `Enter`; it pings you on dangerous ones. The
> safety classifier lives in [`lib/danger.sh`](lib/danger.sh).

## Why

Claude Code gates many actions behind a `Yes / No` permission prompt. That is a
good safety default, but when you run several long sessions it turns into
constant babysitting - and almost every prompt is for something obviously safe
(`grep`, `cat`, reading files, in-repo edits). Relay automates the safe 90% and
escalates - audibly - only the parts that actually need your judgement.

## How it works

`relay` talks to **iTerm2's Python API**: one process watches every iTerm2
session's screen, and for the sessions you **arm**, it auto-clears safe
permission prompts by sending `Enter`. It pings you (notification + sound) on
dangerous commands, real questions, and anything it can't classify. No daemon,
no auto-launch, no shared session-state dir - tool on === TUI open; quit ===
everything stops. (It does keep one durable [audit log](#audit-trail); that's
the only file it writes.)

- **Notify is global.** Any prompt on any tab (armed or not) plays a sound and
  posts a macOS notification - the safe, high-value walk-away half, zero blast
  radius.
- **Inject is narrow.** Only in sessions you've armed, and only when both gates
  below pass.
- While the TUI is open it runs **`caffeinate`** so your Mac (and the armed
  sessions) don't sleep. Quitting releases it. Opt out with `RELAY_NO_CAFFEINATE=1`.

**Why this exists:** Claude Code's built-in command-shape / obfuscation detector
fires permission prompts that **hooks cannot suppress** (they trigger even on
allowlisted commands). Because `relay` acts at the terminal layer, it
*can* clear those.

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

./install.sh                        # checks deps + offers to add bin to PATH
source ~/.zshrc                     # only if it added the PATH line

bin/relay --dry-run           # SAFE FIRST RUN: watch + log, never inject
bin/relay                     # for real
```

> **Relay controls OTHER sessions.** It is a panel for the terminal sessions
> running *around* it - long jobs, Claude Code sessions - not for itself.
> Launching it with only its own tab open shows an empty roster and a
> getting-started panel: that is expected. Open a tab, start a Claude Code
> session or a long command, and it appears in relay's list; arm it with
> `Space` and walk away. (For a coordinated fleet, see [Swarm](#swarm) and
> `relay spawn`.)

`install.sh` only verifies prerequisites and, with your `y`, appends one PATH
line to your shell rc - it installs nothing else. Use `./install.sh --check` to
check without editing anything, and `./uninstall.sh` to remove the PATH line.

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

The **UNIT** column is each session's name: the iTerm2 tab/session name you've
set (Edit Session > Name, or a tab title) if there is one, otherwise iTerm2's
auto, job-derived name.

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

## Swarm

Relay is also a session control plane: named Claude Code sessions register
as **coordinators** or **workers**, send each other messages, and track
tasks (epics with subtasks, states, blockers) - all through one SQLite
database at `~/.relay/relay.db`. No daemon, no event bus. **The DB is the
bus**: swarm CLI verbs write rows and exit; the already-running `relay` TUI
reads the DB on the same tick it uses for screen watching, and delivers.
With the TUI closed, CLI writes still land (messages queue, tasks update) -
delivery just resumes once the TUI is open again, same "tool on === TUI
open" contract as everything else in this repo.

A session binds its identity from `$ITERM_SESSION_ID` (iTerm2 sets this
automatically), so every verb below resolves "me" without you passing an id:

```
relay register --name <name> --role worker|coordinator [--project <p>]
    Bind this session to a swarm name. Re-running rebinds (safe).

relay status "<one line>"
    Update your status line (shown in the relay TUI). Keep it fresh.

relay send <name> "<body>"
    Queue a message for a named session. It is TYPED INTO their Claude
    prompt when they are idle and the relay TUI is running. Single line;
    newlines are flattened.

relay inbox
    Print your undelivered messages and mark them delivered. Check it when
    you start and between tasks (messages may have queued while you worked).

relay msgs [--with <name>] [--project <p>]
    Full message history (delivered + queued).

relay task add "<title>" [--parent <id>] [--owner <name>] [--spec <path>]
               [--blocked-by <id,id>] [--project <p>]
    No --parent = an epic. Assigning --owner to someone ELSE queues them
    an automatic wake-up. --spec points at a spec md file.

relay task update <id> --state todo|doing|blocked|done
    Marking done automatically wakes the owners of tasks that are now
    fully unblocked (all their blockers done).

relay task list [--project <p>] [--mine]
    Epics with nested subtasks, states, owners, blockers.

relay spawn --name <name> "<prompt>" [--project <p>] [--dir <path>]
            [--role worker|coordinator]
    Open a new iTerm2 tab running claude, pre-registered under <name>.
```

Identity-free verbs (`relay task list`, `relay msgs`) work anywhere. The
identity-bound verbs (`register`, `status`, `send`, `inbox`) resolve "me" from
`$ITERM_SESSION_ID`, so they need an iTerm2 session to run. Delivery
additionally requires the TUI running against a real iTerm2 session.

### Delivery

A queued message is only delivered when the target session is **idle at
Claude's input prompt** - the watcher is the one thing that can see this,
same machinery as the permission-prompt gates. Delivery types the message
into the session as its next user turn and hits Enter, prefixed for
provenance:

```
[relay msg from coord] spec ready at specs/be.md
```

A busy target just leaves the message queued for the next idle tick. Two
things auto-generate a message: assigning a task to someone else's
`--owner` (a wake-up naming the task id, title, and spec path), and a task
completing (every task that listed it in `--blocked-by`, once ALL its
blockers are done, wakes its owner). Every delivery is written to the audit
log **before** injection, same contract as auto-approvals - verdicts
`delivered` (live) and `would-deliver` (`--dry-run`, logged but never
typed).

### Staleness

Walking away means a worker can go quiet and you won't notice. Relay flags
a registered session `STALE` (and fires the same notification + sound path
as a dangerous prompt) when either its queued messages have sat undelivered
longer than `RELAY_STALE_MINUTES`, or it owns a `doing` task whose
status/screen hasn't moved in that long. Relay does not auto-reassign the
task or re-prompt the worker - deciding what to do with a stuck worker is
your call; Relay's job is just telling you in time.

### TAB: the swarm view

`TAB` toggles a second, full-width view: a kanban board of tasks by state
(TODO / DOING / BLOCKED / DONE), epic progress, and a recent-messages feed,
filterable by project when more than one is active. The control view
(arm/approve, unchanged) gains **ROLE** and **TASK NOW** columns so you can
see swarm state without leaving it; `STALE` shows right in the STATUS
column.

### relay spawn

`relay spawn --name be-worker --project webshop "..."` opens a new iTerm2
tab, launches `claude` in it with a given first prompt, and pre-registers
the name so you (or a coordinator session) can address it immediately. The
generated first prompt is minimal - it invokes the relay-worker skill and
states name, project, and task; the actual protocol lives in the skill, not
in the spawned prompt. Boot delay before the tab is considered ready is
`RELAY_SPAWN_BOOT_DELAY` seconds.

### Skills

`skills/relay-worker` and `skills/relay-coordinator` are the protocol layer:
what a worker does on start (register, check `relay inbox`, split an
assigned epic into subtasks, keep `relay status` fresh, message the
coordinator when done or blocked) and what a coordinator does (write specs,
create epics with `--owner` and `--spec`, spawn or address named workers,
monitor via `relay task list`). Both skills share one CLI verb reference,
[`skills/relay-cli-reference.md`](skills/relay-cli-reference.md), copied
above. `./install.sh` offers to symlink them into `~/.claude/skills/` so
they version with the repo instead of drifting.

### Security posture (read this)

Two accepted risks that come with the swarm layer:

1. **Prompt-injection surface.** Any local process can `relay send` text
   that becomes another session's next user turn - and an armed session
   will then auto-approve that turn's safe commands. Arm levels remain the
   guardrail; the audit log covers forensics. This is not new in kind (the
   same is true of anything that types at an armed terminal) but the swarm
   layer makes it a first-class, scriptable path, so treat `relay send`
   with the same care as shell access to a machine running an armed
   session.
2. **Input clobbering.** An injected message interrupts anything half-typed
   in the target session's input box. Rare, and accepted rather than
   solved - there's no way to know a human is mid-keystroke from the
   screen alone.

### What's verified, and the one gap

Tested: the gate logic against real captured prompts (incl. the API's NUL/nbsp
cell encoding), live connect/enumerate/stream/teardown, and the TUI render +
controls headless. **Not** yet exercised end-to-end on a live *fired* prompt -
that's precisely what `--dry-run` is for. The `danger.sh` Track-2 leader gaps
(above) apply; see [`test/danger_test.sh`](test/danger_test.sh).

## Configuration

Environment variables (set before launching `relay`):

| Variable                     | Default                    | Purpose                                   |
| ---------------------------- | -------------------------- | ----------------------------------------- |
| `RELAY_AUDIT_LOG`            | `~/.relay/audit.jsonl`     | Where the audit trail is written          |
| `RELAY_AUDIT_RETENTION_DAYS` | `7`                        | Days of audit history kept at launch      |
| `RELAY_NOTIFY_COOLDOWN`      | `30`                       | Min seconds between alerts per session    |
| `RELAY_NO_CAFFEINATE`        | unset                      | Set to `1` to not keep the Mac awake      |
| `RELAY_NO_REACTOR`           | unset                      | Set to `1` to hide the reactor meter      |
| `RELAY_DB`                   | `~/.relay/relay.db`        | Swarm SQLite file (sessions/messages/tasks) |
| `RELAY_STALE_MINUTES`        | `10`                       | Minutes of no progress before STALE fires |
| `RELAY_SPAWN_BOOT_DELAY`     | `6.0`                      | Seconds `relay spawn` waits for the tab to boot |

Two of these - `RELAY_STALE_MINUTES` and `RELAY_NOTIFY_COOLDOWN` - also have a
home in the config file below. **Precedence: defaults < config file <
environment variable.** The env var always wins, so anything already set in
your shell keeps working unchanged.

Risk posture (which commands auto-approve vs escalate) is edited directly in
[`lib/danger.sh`](lib/danger.sh).

### Configuration file

`~/.relay/config`, INI format (override the path with `RELAY_CONFIG`). Read
once at startup. A missing file, missing section, or missing key silently
falls back to the default; a malformed file logs one warning line and falls
back too - it never crashes the TUI.

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
spawn_arm       = off  ; arm level for spawned workers: off | safe | wild | insane
                       ; honored only at FIRST sight of a session (spawn's boot
                       ; window); a request appearing later for a known session
                       ; is refused and escalated (self-escalation guard)
```

Deliberately not configurable here: bootstrap paths (`RELAY_DB`,
`RELAY_CONFIG`), session-scoped flags (`RELAY_NO_CAFFEINATE`,
`RELAY_NO_REACTOR`, `--dry-run`), the spawn boot delay, `lib/danger.sh`'s
rules (own home), and the title glyph/word vocabulary (it doubles as the
strip-parser - a configurable vocabulary would double the bug surface).

### Tab-title prefixes

Set `[titles] style` and Relay rewrites the iTerm2 tab title itself, so arm
mode and attention state are glanceable on the tab bar without opening the
TUI - `✦[BLOCKED] api-server`.

| situation          | glyphs   | words                    | hybrid            |
| ------------------ | -------- | ------------------------ | ----------------- |
| safe, working      | `◉ api`  | `[SAFE] api`             | `◉ api`           |
| insane, blocked    | `✦⊘ api` | `[INSANE][BLOCKED] api`  | `✦[BLOCKED] api`  |
| safe, prompting    | `◉‼ api` | `[SAFE][AWAITING] api`   | `◉[AWAITING] api` |
| armed, stale       | `◉⧗ api` | `[SAFE][STALE] api`      | `◉[STALE] api`    |
| manual, blocked    | `⊘ api`  | `[BLOCKED] api`          | `[BLOCKED] api`   |
| manual, idle       | `api`    | `api`                    | `api`             |

Relay only writes a title for a session that's **armed (any level) or in an
attention state** (prompting, blocked, or stale) - a manual, idle tab is left
untouched. If a manual+idle session was previously prefixed, Relay writes the
bare name back once and then leaves it alone. On quit, Relay restores the
bare name on every session it wrote to during that run (best-effort - a
session may already be closed). `style = off` is fully inert on the write
path (it still strips on read, so a leftover prefix from an old run gets
cleaned up), and titles are **never touched in `--dry-run`** - the same
"dry-run mutates nothing" guarantee as everything else in this repo.

**Crash honesty:** if relay dies without restoring, a prefix lingers on the
tab bar. The next run self-heals it **only** for a tab that is armed or in an
attention state - its write path recomputes the prefix and rewrites/restores.
A **manual, idle** tab is deliberately never rewritten (that protects titles
you set by hand), so a leftover prefix there persists until you rename the tab
yourself or briefly arm it (which lets relay take ownership and then restore
the bare name). Reads are always clean regardless - the UNIT column and swarm
addressing strip the prefix on read. Same residue class as any other tool that
writes tab titles.

## Project layout

```
relay/
  bin/relay        # launcher
  iterm/app.py           # Textual TUI (the control panel + swarm view)
  iterm/watcher.py       # iTerm2 connection: stream screens, run gates, inject, deliver
  iterm/gates.py         # pure gate logic (type + safety), no iTerm2 imports
  iterm/audit.py         # durable audit log of unattended decisions
  iterm/config.py        # ~/.relay/config loader (titles/sounds/swarm), pure stdlib
  iterm/titles.py        # tab-title render/strip, pure, no iTerm2 imports
  iterm/db.py            # swarm SQLite schema + connection (~/.relay/relay.db)
  iterm/swarm.py         # pure swarm logic: delivery text, staleness, rendering
  iterm/cli.py           # swarm CLI verbs (register, send, task, inbox, ...)
  iterm/spawn.py         # relay spawn: new iTerm2 tab + claude + pre-registration
  iterm/test_*.py        # gate/TUI/swarm suites, built from real captured prompts
  iterm/test_config.py   # config loader tests (temp files, precedence)
  iterm/test_titles.py   # render/strip round-trip tests
  iterm/test_db.py       # swarm schema + query tests (temp DB file)
  iterm/test_swarm.py    # delivery/staleness/rendering logic tests
  iterm/test_cli.py      # CLI verb tests against a temp DB file
  lib/danger.sh          # shared command-classification rules (tune me)
  test/danger_test.sh    # classifier regression suite (run before tuning danger.sh)
  test/run.sh            # run the whole suite (bash + Python), no pytest needed
  install.sh             # prerequisite check + optional PATH/skills setup
  uninstall.sh           # removes Relay's PATH line
  skills/                # relay-worker, relay-coordinator (symlinked by install.sh)
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

## License

MIT - see [LICENSE](LICENSE).
