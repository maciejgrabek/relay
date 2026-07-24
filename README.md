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
  RELAY · SESSION CONTROL · 3 units · 2 armed · 12✓ 1⊘ · 1 awaiting · 2 msgs queued
  CORE TEMP ▰▰▰▱▱▱▱▱▱▱  ◷ WARM

  MODE      STATUS      ↻    UNIT          ROLE   TASK NOW     ✓/⊘  LAST DIRECTIVE
  ── NEEDS ACTION (1) ──────────────────────────────────────────────────────────
▸ ✦ INSANE  ‼ AWAITING  4s   ‼ api-worker  work   #17 ⊘ by 14  2/1  terraform apply -auto-…
  ── SESSIONS ──────────────────────────────────────────────────────────────────
  ◉ SAFE    ▸ ACTIVE    12s  bff-worker    work   #14 doing    5/0  grep -rn "TODO" src/
  ✦ INSANE  ‼ AWAITING  4s   api-worker    work   #17 ⊘ by 14  2/1  terraform apply -auto-…
  ○ MANUAL  ◌ STANDBY   3m   coord         coord  specs 3/3    -    -
  ──────────── live terminal feed of the selected session shows below ────────────

  ↑↓ move · SPACE arm · ENTER answer · 1/2/3 send · n go to tab · x hide
  a arm all · d disarm all · TAB swarm · R×2 restore · W×2 wipe · q quit
```

The list is on top and the selected session's **live terminal feed** is stacked
below it, both full-width. `TAB` flips to the **swarm view** (a kanban board of
tasks + a message feed) when you're running a coordinated fleet.

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
   where that matters - or set **`[danger] preset = paranoid`**, which flips
   `safe` mode to default-deny: only read-only leading commands auto-approve,
   everything else (including all the leader gaps above) escalates. More
   pings, much smaller blast radius.

## Requirements

- macOS (uses `afplay`, `caffeinate`, and `osascript` notifications)
- iTerm2 with the **Python API enabled** (Settings -> General -> Magic -> Enable
  Python API)
- Python 3 with the `iterm2` and `textual` modules
- Optional: `terminal-notifier` (`brew install terminal-notifier`). With it,
  notifications show as **iTerm** and clicking one jumps to the exact session it
  is about. Without it they fall back to `osascript`, which shows "Script
  Editor" and has no click action. `relay doctor` reports which you have.

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

**See the whole loop in 60 seconds:** with the panel running, open another
tab and run `relay demo` - it registers that session as a demo coordinator,
spawns one armed worker in a temp dir, assigns it a haiku task, and tells
you exactly what to watch (the worker arming itself, the task moving on the
board, the haiku typed back into your prompt). Clean up with
`relay wipe --project demo --all --yes`.

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
| `s` | **Shadow-arm** the selected tab: dry-run only, never acts (see below) |
| `p` | **Pause / resume** relay's acting: freezes approvals + deliveries, keeps watching (see below) |
| `,` | Open the **settings editor** (see below) |
| `n` | Go to (focus) the real iTerm2 tab for the selected session |
| `x` | Hide / show the selected session |
| `v` | **Audit view**: the selected session's record of unattended decisions (approvals, escalations, deliveries) in the feed pane; `v` again returns to the live feed |
| `?` | Help overlay: key map + arm-level cheat sheet |
| `TAB` | Toggle the **swarm view** (kanban board + message feed) |
| `R` `R` | **Press twice:** restore dead workers (respawn in their workdir) |
| `W` `W` | **Press twice:** wipe dead sessions' work (delete). Guarded by the double-press |
| `q` | Quit (tears down the iTerm2 connection, releases `caffeinate`). Instant when idle; when sessions are armed or swarm work is live (queued messages, `doing` tasks) it asks for a **second `q`** within 5s - same confirm pattern as `R`/`W`, because quitting stops auto-approval and delivery |

`R` and `W` only act when a worker's tab has closed while it still owned tasks;
the panel shows a red hint and the count when that happens. The double-press is
the confirm - the first press arms it (auto-cancels after 5s), the second fires.

Hidden sessions don't vanish - they drop to a dimmed section at the bottom of the
list, and the cursor skips the divider as you navigate, so you fly between your
kept sessions while still being able to see and un-hide (`x` again) the rest. The
**live feed** pane below the list pulls the selected session's current screen the
moment you land on it (and updates as that session prints), so you see the live
prompt before you answer it.

The **UNIT** column is each session's name: the iTerm2 tab/session name you've
set (Edit Session > Name, or a tab title) if there is one, otherwise iTerm2's
auto, job-derived name. Relay's own tab is named **`RELAY CONSOLE`** and
**colored relay-green** in the tab bar while the panel runs (otherwise it
would show its `caffeinate` child); name and color are handed back to
iTerm2's defaults on quit (session-scoped - your profile is never modified),
and never touched in `--dry-run`.

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

## Examples

### Walk away from a few long jobs

```bash
# 1. Open 2-3 iTerm2 tabs and start whatever you want babysat -
#    a build, a test loop, a Claude Code session working a task.
# 2. Launch the panel:
relay
# 3. Cursor to a tab you trust, press SPACE to arm it (◉ SAFE).
#    Relay now auto-clears that tab's safe permission prompts and
#    pings you (sound + notification) on anything dangerous.
# 4. Walk away. Come back when you hear a ping, or check the audit log:
tail -f ~/.relay/audit.jsonl
```

With only relay's own tab open you get a getting-started panel - that is
expected; relay controls the sessions *around* it.

### Try the swarm (a 5-minute smoke test)

You can exercise the whole loop from **one Claude Code session** - it plays the
coordinator, drives the verbs through its Bash tool, and *sees the worker's
reply arrive in its own prompt*, so it can confirm success itself. Prereqs:
you're inside iTerm2 and `relay` is running in another tab.

From the Claude session (or your shell) as coordinator:

```bash
relay register --name coord --role coordinator --project smoke

# spawn an armed worker in a throwaway dir (a new tab opens):
relay spawn --name w1 --project smoke --dir /tmp --arm wild \
  "await your task via relay inbox, then do it"

# give it a trivial task:
relay task add "write a haiku about terminals, then relay send coord the haiku" \
  --owner w1 --project smoke
```

**What success looks like**, in order:

1. In the relay panel, `w1` flips to `✦ INSANE`/`▲ WILD` within a few seconds
   (spawn pre-arming applied by the watcher).
2. relay types the assignment into `w1`'s idle prompt; `w1` writes the haiku.
3. `w1`'s haiku is **typed into this coordinator session's prompt** as a
   `[relay msg from w1] ...` turn - the coordinator observes the reply directly.
4. `TAB` in the panel shows the task move `todo -> doing -> done`.

Then clean up: `relay wipe --project smoke --all --yes`.

If it stalls, `relay doctor` shows where (worker not armed? message queued but
undelivered because the panel is not running? task stuck in `doing`?). This is
the same hand-check behind the "live paths" note at the top of this section.

### Run a coordinated swarm

From one session, become the coordinator, spawn armed workers, hand out
work, and let relay ferry the messages:

```bash
# in a Claude Code session (or your shell), register as coordinator:
relay register --name coord --role coordinator --project webshop

# spawn two workers, each ARMED so it can act unattended (a new tab per worker):
relay spawn --name bff  --project webshop --dir ~/work/bff --arm wild "await your task"
relay spawn --name api  --project webshop --dir ~/work/api --arm wild "await your task"

# two workers on the SAME repo: add --worktree so each gets its own git
# worktree (branch relay/<name>, sibling dir <repo>-<name>) instead of
# clobbering one working copy:
relay spawn --name api2 --project webshop --dir ~/work/api --worktree --arm wild "await your task"

# assign an epic to each - the owner is woken automatically with the task:
relay task add "add /checkout endpoint" --owner bff --project webshop
relay task add "checkout order model"   --owner api --project webshop \
               --blocked-by 1     # api's task waits until the bff task is done

# launch the panel and watch it happen (TAB toggles the swarm board):
relay
```

Workers report back by messaging the coordinator; those messages are typed
into the coordinator session's prompt when it is idle. When the bff task is
marked done, relay automatically wakes the api worker (its blocker cleared).

### Check swarm health without the TUI

If you launched relay and feel stuck, or a worker seems frozen, ask from any
shell:

```bash
relay doctor
# relay <sha> <date>
#   sessions: 2 registered   (bff mode=wild doing #1, api mode=wild ...)
#   messages: 0 queued
#   tasks: 1 doing, 1 blocked
#   !! possible stall: #1 'add /checkout endpoint' doing, no update in 22m
```

`relay doctor` reads the database only - it never changes anything - and
flags the two things that silently trap people: messages piling up
undelivered (the panel is not running) and tasks stuck in `doing`.

### See what relay did

```bash
relay recap
# relay recap (today)
#   cleared 12 · woke you 1x · delivered 3
#   tasks: 4 done · 1 doing · 0 blocked · 2 todo
```

`relay recap` prints a one-line summary of today's activity (commands
cleared, how many times it woke you, tasks done); `relay recap --all` covers
all time instead of just today. It only reads the audit log and task board -
same read-only contract as `relay doctor`. The panel also prints this line
for you automatically when you quit.

### Update to the latest version

Launching the TUI **self-updates first**: `bin/relay` runs a quiet
fast-forward check before the app boots (at most once a day), so the code
that starts is current and any DB migration applies on that same launch. It
is silent when offline, up to date, or the checkout is dirty/diverged - a
version check never delays or blocks a launch - and it prints one line when
it actually updated. `--dry-run` skips it (dry-run mutates nothing, the
checkout included); `RELAY_NO_AUTOUPDATE=1` disables it entirely. Manually:

```bash
relay version          # what you have now
relay update           # fetch + fast-forward (safe: stops on local changes)
```

## Swarm

> **Status: newer and less battle-tested than the arm/approve core.** The
> swarm's DB, CLI, delivery, staleness, and recovery logic are unit-tested,
> but the *live* paths - spawning a worker, typing a message into a real idle
> session, restore/clean/wipe against actual tabs - are checked by hand, not
> in CI (that is the nature of driving iTerm2). It works (the examples below
> are real runs), but expect rougher edges than the approval half: keep
> `--dry-run` and the confirmation prompts in the loop, and reach for
> `relay doctor` when a worker seems stuck. To confirm your own setup drives
> the full loop, run the 5-minute smoke test under
> [Examples](#try-the-swarm-a-5-minute-smoke-test). Tab-side arm/disarm from an
> iTerm2 status-bar component is designed (`docs/drafts/`) but not yet built.

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

relay send <name> "<body>" [--kind <k>]
relay send --all --project <p> "<body>" [--kind <k>]
    Queue a message for a named session (or every live session in the
    project except you, with --all). Delivered TYPED INTO their Claude
    prompt when they are idle and the relay TUI is running. Single line;
    newlines are flattened. --kind: info (default) | done | blocked |
    escalation | a custom lowercase token ('wake' is reserved). escalation
    also pings the human immediately.

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
            [--role worker|coordinator] [--worktree]
    Open a new iTerm2 tab running claude, pre-registered under <name>.
    --worktree (with --dir <repo>): spawn in a fresh git worktree of that
    repo instead of the repo itself.
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

### Message kinds

`relay send` and `relay send --all` take `--kind`: `info` (default) |
`done` | `blocked` | `escalation` | a custom lowercase token. The kind
shows up in the delivery prefix, e.g. a `done` message from `bff` arrives
as:

```
[relay done from bff] task #4 done on branch relay/bff: /checkout endpoint added
```

(a plain `info` message still prefixes as `[relay msg from <name>]`, for
backward compatibility.) `escalation` additionally plays the alert sound
and posts a macOS notification for the human **immediately** on send -
before the target session is even idle - so reserve it for messages that
genuinely need a human's judgment, not routine coordinator back-and-forth.
`wake` is reserved for relay's own automatic wake-ups (task assignment,
unblocked-task notices) and cannot be passed to `--kind`.

### Staleness

Walking away means a worker can go quiet and you won't notice. Relay flags
a registered session `STALE` (and fires the same notification + sound path
as a dangerous prompt) when either its queued messages have sat undelivered
longer than `RELAY_STALE_MINUTES`, or it owns a `doing` task whose
status/screen hasn't moved in that long. Relay does not auto-reassign the
task or re-prompt the worker - deciding what to do with a stuck worker is
your call; Relay's job is just telling you in time.

### TAB: the swarm view

`TAB` toggles a second, full-width view:

- a **FLEET line** on top - unit counts by state (busy / blocked / idle),
  armed counts by mode, stale count, queued messages;
- the roster with a per-worker **heartbeat** (`↻ 12s` since its screen last
  moved; a stale row renders red with `⧗`);
- a kanban board of tasks by state (TODO / DOING / BLOCKED / DONE) and epic
  **progress bars** (`▰▰▰▰▱▱▱▱  4/8`);
- an **INTERACTIONS** map - who talks to whom: per-pair sent/received
  counts, last message kind and age, `‼` when the pair's last word was
  `blocked` or `escalation`;
- the recent-messages feed, **colored by kind** (done green, blocked
  yellow, escalation red, wake dim).

The control view keeps **ROLE** and **TASK NOW** columns and shows sessions
that need a human (prompting, blocked, or stale) as **duplicate rows in a
NEEDS ACTION strip** on top - the main list below **never reorders**, so
your muscle memory holds; the duplicate simply disappears once you act, and
the original row stays exactly where it was. Arrow keys walk continuously:
down goes through the strip, then the full list (dividers are skipped), up
walks it back. Strip rows are fully interactive (navigate, answer, arm).
The view also shows a per-tab heartbeat in the `↻` column, and the
live-feed pane names WHY the selected session is being held
(`‼ AWAITING: <command>`); the held command renders red in LAST DIRECTIVE.

### relay spawn

`relay spawn --name be-worker --project webshop "..."` opens a new iTerm2
tab, launches `claude` in it with a given first prompt, and pre-registers
the name so you (or a coordinator session) can address it immediately. The
generated first prompt is minimal - it invokes the relay-worker skill and
states name, project, and task; the actual protocol lives in the skill, not
in the spawned prompt. Boot delay before the tab is considered ready is
`RELAY_SPAWN_BOOT_DELAY` seconds.

Add `--worktree` (requires `--dir <repo>`) to create branch `relay/<name>`
and a sibling git worktree `<repo>-<name>`, then spawn the worker there
instead of in `<repo>` itself. Use it whenever two or more workers will
touch the same repo, so their edits can't clobber each other; `relay wipe`
cleans up the worktree later (see below).

### Recovering abandoned work

A registered session is **closed** (dead) once its iTerm2 tab is gone for
several consecutive watcher ticks in a row - the debounce stops a transient
empty roster from false-marking a live swarm. A closed session that still
owns non-`done` tasks is an **orphan**: work assigned to nobody who can do
it. `relay doctor` prints an `orphans:` line listing each one and its last
known workdir; the TUI shows the same count as a red hint in the subtitle
(`N task-owner(s) dead - press R to restore, or run 'relay clean'`) whenever
one exists.

Three ways to deal with an orphan - pick by disposition: `restore` =
continue the work, `clean` = reset it back to unowned todo, `wipe` = DELETE
it and there is no undo.

```
relay restore [names...] [--project <p>] [--dry-run] [--yes]
    Respawn dead workers in the workdir they were spawned in (recorded by
    `relay spawn --dir`), with a resume prompt pointing back at
    `relay task list --mine` and `relay inbox`. Always prints a PLAN first;
    without --yes it asks to confirm before spawning anything, and
    --dry-run stops after the plan.

    No names = every CLOSED session that owns non-done work. Named =
    those specific sessions even if their tab is still open (STALLED but
    not closed) - useful when a worker is wedged, not gone. Restoring a
    session whose tab is still open leaves the old tab running as a
    zombie; kill it yourself once the new one is up.

    A candidate with no recorded workdir (registered before this
    feature, or never spawned via `relay spawn --dir`) is SKIPPED, not
    guessed at - the plan tells you to `relay clean` it or re-run relay
    from the right directory. If `[swarm] spawn_arm` is `off`, the plan
    also warns that restored workers come back unarmed and won't act
    unattended until you arm them.

relay clean [--project <p>] [--dry-run] [--yes]
    The OPPOSITE of restore: resets every non-done task owned by a closed
    session back to unowned todo, then deletes the closed session row
    (and its undelivered messages). It destroys exactly the workdir
    context that restore needs, so if you're not sure which one you
    want, run `relay restore` first - `relay clean` is for orphans you've
    decided are not worth reviving.

relay wipe [names...] [--project <p>] [--all] [--dry-run] [--yes]
    The delete-counterpart to clean: instead of resetting a closed
    session's non-done tasks to todo, it DELETES those tasks outright
    (any state, including done), then deletes the session row and its
    undelivered messages. Same candidate set as clean - no names =
    every closed session (including ones that own no tasks), named =
    those specific closed sessions. Live sessions are never touched by
    the orphaned form. For a session spawned with `--worktree`, wiping it
    also removes its git worktree and `relay/<name>` branch - but only
    when the worktree is clean; a dirty worktree (uncommitted or untracked
    changes) is always kept so in-progress work is never silently deleted.

    `--all` requires `--project <p>` and nukes that whole project in one
    shot: every task, session (live or closed), and message it has, no
    owner filter at all. It's the "start this project over from nothing"
    button. `--all` without `--project` is refused outright, so you can't
    wipe every project on the machine by accident.

    Like restore and clean, it always prints a WIPE PLAN first (task and
    session counts, or the project totals for --all), then asks to
    confirm unless --yes; --dry-run prints the plan and stops there.
    Before deleting, it also checks whether any task being wiped is a
    blocker for a task that ISN'T being wiped, and prints a WARNING per
    case - that dependent may never unblock once its blocker is gone, so
    you'll want to clear its `blocked_by` by hand afterward.

    There is no undo. If you're not sure whether an orphan's work is
    worth keeping, use `relay clean` instead - it leaves the task rows in
    place as todo so you can still see and reassign them.
```

In the TUI, press `R` to restore every closed orphan in one shot: the first
press arms a 3-second confirm window (a log line says so), a second `R`
inside that window shells out to `relay restore --yes` in the background.
Press `W` the same way to wipe every closed orphan's work (orphaned scope
only - there's no TUI binding for `--project --all`, that's deliberately a
terminal-only, type-it-out command). `relay clean` has no TUI binding;
run it from a terminal when you've decided the work is not worth reviving.

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
| `RELAY_LOCK`                 | `~/.relay/relay.lock`      | Single-instance lock (one panel at a time) |
| `RELAY_STALE_MINUTES`        | `10`                       | Minutes of no progress before STALE fires |
| `RELAY_SPAWN_BOOT_DELAY`     | `6.0`                      | Seconds `relay spawn` waits for the tab to boot |
| `RELAY_MSG_RETENTION_DAYS`   | `7`                        | Days a delivered message is kept before pruning |
| `RELAY_NO_AUTOUPDATE`        | unset                      | Set to `1` to skip the TUI's start-up self-update |
| `RELAY_STATUSBAR_STATE`      | `~/.relay/statusbar.json`  | Badge state relay publishes for the AutoLaunch provider |
| `RELAY_STATUSBAR_CLICKS`     | `~/.relay/statusbar-clicks.jsonl` | Badge-click queue the provider writes, relay consumes |
| `RELAY_STATUSBAR_ALIVE`      | `~/.relay/statusbar-provider.alive` | Provider heartbeat (relay registers its own badge unless fresh) |
| `RELAY_DANGER_PRESET`        | from `[danger] preset`     | `default`/`paranoid` - env wins over the config file |

> **Keep `~/.relay/` on a local disk, not a synced folder** (iCloud Drive,
> Dropbox, a network mount). Relay's SQLite DB uses WAL mode; a background
> sync process copying the `.db`/`.db-wal` files mid-write can corrupt them.
> If you must relocate it, point `RELAY_DB` at a local path.
>
> Only one relay panel runs at a time (an advisory lock at `RELAY_LOCK`) -
> two would each deliver every queued message, typing each wake-up twice. A
> second `relay` exits with a message telling you the first is still up. The
> lock is a kernel `flock`, so it releases automatically if relay exits for
> ANY reason - clean quit, crash, or `kill -9` - and a fresh `relay` starts
> normally afterward. There is no stale-lock trap to clear by hand.

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

[statusbar]
enabled = true         ; register a per-tab arm badge in iTerm2's status bar

[danger]
preset = default       ; default | paranoid. paranoid flips 'safe' mode to
                       ; DEFAULT-DENY: only read-only leading commands
                       ; (ls/cat/grep/git log/...) auto-approve - closes the
                       ; make/npm/python leader gaps at the cost of far more
                       ; escalations

[theme]
name = phosphor        ; phosphor | amber | ice - recolors the whole TUI

[layout]
preview = true         ; show the live-feed pane under the list (default true);
                       ; toggle live with f, or here in the settings editor
```

Deliberately not configurable here: bootstrap paths (`RELAY_DB`,
`RELAY_CONFIG`), session-scoped flags (`RELAY_NO_CAFFEINATE`,
`RELAY_NO_REACTOR`, `--dry-run`), the spawn boot delay, `lib/danger.sh`'s
rules (own home), and the title glyph/word vocabulary (it doubles as the
strip-parser - a configurable vocabulary would double the bug surface).

### Sounds and the settings editor

Relay uses four distinct sounds so your ear can triage without looking, all set
in `[sounds]` (any can be set empty to silence it):

| Key | Fires on | Default |
| --- | -------- | ------- |
| `danger` | a session about to run a dangerous command | Basso |
| `alert` | needs a look (real question, stale session, error) | Sosumi |
| `message` | a swarm worker messaged / escalated to you | Tink |
| `done` | a task or epic completed | Glass |

Press **`,`** in the panel to open the **settings editor**: `↑`/`↓` move
between settings, `←`/`→` change the highlighted one, `p` auditions the
highlighted sound, and every change is saved to `~/.relay/config` as you go -
no separate save step. Sound changes apply immediately; the rest take effect
on the next relay start (the editor tags those fields "restart to apply"). On
Apple Silicon the status-bar badge also needs Rosetta 2 - `relay doctor`
checks it.

Note: `RELAY_STALE_MINUTES` and `RELAY_NOTIFY_COOLDOWN` override the config
file when set in your shell environment, so if either is exported, editing
the matching setting in this panel is saved to the file but has no effect
until you unset the environment variable.

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

### iTerm2 status-bar arm badge

Relay can put a per-tab **arm badge** in iTerm2's own status bar (the strip
with your CPU / memory / network components), so you see and change a tab's arm
level from the tab itself - no need to switch to the panel. It is **off by
default**; enable it with `[statusbar] enabled = true` in `~/.relay/config`.

Each tab's badge shows a colored circle for the arm mode plus `RELAY:<mode>`,
and appends the swarm identity when the tab is a registered coordinator/worker:

```
⚪ RELAY:off                     a manual tab
🟢 RELAY:safe                    armed safe
🟡 RELAY:wild                    armed wild
🔴 RELAY:insane                  armed insane
🟢 RELAY:safe · bff-worker (work)   a swarm worker
⬛ RELAY: panel                  relay's own tab (inert - relay never arms itself)
⚫ RELAY: off                    relay itself is not running
```

(The color comes from the emoji circle: iTerm2's status-bar API returns plain
text, so a colored glyph is how you get color-per-mode.)

**Click a badge to cycle its arm level** - `off -> safe -> wild -> insane -> off`,
exactly what `Space` does in the panel, and the panel row updates in lockstep
(the badge reads and writes relay's real state, not a copy). A click is a
physical human action - a Claude session cannot click a status bar - and
clicking the `⚫ RELAY: off` badge (relay closed) does nothing. Relay's own
panel tab shows `⬛ RELAY: panel` and its click does nothing either.

**How it stays error-free when relay is off:** iTerm2 keeps the component in
your profile once you add it, and renders a component with no provider as an
ERROR. So the badge is served by a tiny **AutoLaunch provider**
(`iterm/statusbar_autolaunch.py`, symlinked by `install.sh` into iTerm2's
`Scripts/AutoLaunch/`), which iTerm2 runs itself: while relay is up it shows
the per-tab state relay publishes each tick (`~/.relay/statusbar.json`, wiped
on quit), and with relay off it shows `⚫ RELAY: off` instead of an error.
Clicks are queued to `~/.relay/` and applied by the running relay with its
normal guards; writes into `~/.relay` classify DANGEROUS in `lib/danger.sh`,
so a safe-mode session cannot forge a click.

**One owner, no freeze:** exactly one thing may register the badge - iTerm2
rejects a second registration of the same component (`com.relay.arm`) with
`DUPLICATE_SERVER_ORIGINATED_RPC`, which leaves the badge frozen on a stale
frame (e.g. stuck on `⚫ RELAY: off` even after you start relay). So relay
decides who owns the badge by a stable fact - **is the AutoLaunch provider
installed (its symlink present)?**

- **Provider installed** -> the provider owns the badge; relay never registers,
  it only publishes state and applies clicks. Restart iTerm2 (or start the
  script once) so the provider is actually running.
- **Provider absent** -> relay registers the badge in-process as the sole
  owner. Zero setup, but the slot shows an iTerm2 error whenever relay is
  closed - install the provider to fix that.

(relay keys this on the symlink, not the provider's heartbeat: the heartbeat
lags a just-launched provider, so keying on it made relay briefly double-
register and freeze the badge.)

**Adding it (one-time):** run `./install.sh` and answer yes to the AutoLaunch
symlink, start it once (iTerm2 menu **Scripts -> AutoLaunch ->
relay_statusbar.py**, or restart iTerm2), set `[statusbar] enabled = true`,
then open iTerm2 **Settings -> Profiles -> your profile -> Session ->
Configure Status Bar** (enable "Status bar enabled" if needed) and drag the
**"Relay"** component into the bar.

> **Apple Silicon needs Rosetta 2.** The AutoLaunch provider runs under
> iTerm2's bundled Python runtime, which is x86_64 - so on an M-series Mac the
> provider (and therefore the badge) silently never starts unless Rosetta 2 is
> installed: `softwareupdate --install-rosetta --agree-to-license`. `relay
> doctor` checks this for you.

> **"Relay" isn't in the Configure Status Bar list?** The component only
> appears in that picker while a provider is **registered** - i.e. the
> AutoLaunch provider is running, or (with no provider installed) relay is
> running. An empty list means nothing is registered right now, not that it's
> broken: start the provider (restart iTerm2) and reopen the picker. Run
> **`relay doctor`** for a checklist of the three steps - enabled / installed /
> running - and exactly which one is missing.

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
  iterm/statusbar.py     # status-bar badge: pure labels + published state / click queue
  iterm/statusbar_autolaunch.py  # always-on badge provider (symlinked into iTerm2 AutoLaunch)
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
