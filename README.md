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
  RELAY · SESSION CONTROL · 3 units · 2 armed · 12✓ 1⊘ · 1 awaiting · 2 msgs queued · 3 parked
  CORE TEMP ▰▰▰▱▱▱▱▱▱▱  ◷ WARM

  MODE      STATUS      ↻    CTX  SESSION       ROLE   TASK NOW     ✓/⊘  LAST DIRECTIVE
  ── NEEDS ACTION (1) ──────────────────────────────────────────────────────────
▸ ✦ INSANE  ‼ AWAITING  4s   91%  ‼ api-worker  work   #17 ⊘ by 14  2/1  terraform apply -auto-…
  ── SESSIONS ──────────────────────────────────────────────────────────────────
  ◉ SAFE    ▸ ACTIVE    12s  62%  bff-worker    work   #14 doing    5/0  grep -rn "TODO" src/
  ✦ INSANE  ‼ AWAITING  4s   91%  api-worker    work   #17 ⊘ by 14  2/1  terraform apply -auto-…
  ○ MANUAL  ◌ STANDBY   3m   -    coord         coord  specs 3/3    -    -
  ──────────── live terminal feed of the selected session shows below ────────────

  ↑↓ move · SPACE arm · s shadow · ENTER answer · 1/2/3 send · n go to tab · x hide · i park · b parked · v audit · f feed · t timers · E×2 extreme
  a arm all · d disarm all · TAB swarm · p pause · ! stop/tell · , settings · R×2 restore · W×2 wipe · Z×2 zap · ? help · q quit
```

The list is on top and the selected session's **live terminal feed** is stacked
below it, both full-width. `TAB` flips to the **swarm view** (a kanban board of
tasks, open discussions, pull requests and a message feed) when you're running
a coordinated fleet.

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
everything stops.

That last rule still holds with the [desktop widget](#the-desktop-widget), but
it is worth being precise now that a second process exists: relay starts the
widget and kills it on quit, so quitting still stops everything. The widget only
ever reads; it cannot arm, approve, pause or inject. Files relay writes: one
durable [audit log](#audit-trail), the swarm DB, and two published-state files
(`~/.relay/statusbar.json`, `~/.relay/widget.json`) that exist so out-of-process
consumers can render relay's state without asking it anything.

- **Notify is global.** Any prompt on any tab (armed or not) plays a sound and
  posts a macOS notification - the safe, high-value walk-away half, zero blast
  radius.
- **Inject is narrow.** Only in sessions you've armed, and only when both gates
  below pass.
- While the TUI is open it runs **`caffeinate`** so your Mac (and the armed
  sessions) don't sleep. Quitting releases it, `c` releases it by hand, and
  `[power] release_after` releases it automatically once the whole fleet has
  been idle that many minutes (default `0`, never). Opt out entirely with
  `RELAY_NO_CAFFEINATE=1`. Releasing is not sleeping - relay stops *preventing*
  sleep and hands the decision back to macOS, which knows whether you're here.

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
  detector prompts `safe` can't read get cleared. Read that literally: wild does
  not consult `lib/danger.sh` at all, so a command the classifier calls
  **DANGEROUS** is approved like any other. Wild is not "safe plus a bit" - it
  is insane minus one case (below).
- **insane** (`✦ INSANE`) - approves **any** tool-permission prompt at all, even
  the fail-safe cases (cursor not on option 1, unparseable command). In practice
  that is wild plus exactly one case: acting when the cursor is **not** on the
  affirmative default.

The ladder's ordering is a safety property, not a description: every rung
approves a superset of the rung below it, so a more cautious mode can never
clear something a less cautious one refuses. `gates.mode_approves` is the whole
policy, and `test_gates.py` enumerates every decision the classifier can emit
to prove the ordering still holds. No mode auto-answers a real multi-choice
question - those carry `is_permission=False`, which is the floor under all of
it.

A fifth level sits above insane and is armed only via `E E` in the TUI:

- **extreme** (`✷ EXTREME`) - insane mode + idle pushes: arms on an insane session
  only via double-press `E E`; pushes the configured prompt into an idle tab after
  `extreme_dwell` seconds; fires budget-capped by `extreme_fires` then reverts
  to insane; disarmed by relay restart.

**A real question (multi-choice, no proceed-marker) is ALWAYS handed off to
you - NO mode auto-answers your decisions.**

Use `safe` where a wrong Enter would hurt; `wild`/`insane` in scratch/throwaway
workspaces where you just want the friction gone.

### Intervene (`!`)

Extreme mode pushes a prompt into an idle tab with nobody watching, and until
now relay had no way to reach into a session and stop it. `!` on the selected
row opens a modal with a scope and three modes, counts filled in before you
commit.

- **STOP** sends a bare `ESC` right away to every targeted session that is
  still working - interrupts mid-turn. Idle targets are skipped; there's
  nothing to interrupt.
- **TELL** types the message straight into every targeted session and hits
  Enter, right away - the same mechanism `STOP`'s `ESC` uses, and the same
  one behind relay's manual `1`/`2`/`3`/`ENTER` sends. It works on any tab,
  registered or not: it's keyed on the session, not a swarm name. It does
  not interrupt - a working session just gets the text sitting in its input
  box, same as if you'd typed it yourself.
- **STOP + TELL** does both, in the same pass: the `ESC` first (on working
  targets), then the message and its return.

Nothing here is queued, and nothing waits for a session to go idle - both
modes act the moment you press `ENTER` on the modal.

`TAB` cycles mode, `←`/`→` cycles scope through selected / project / all, and
the counts update on every scope change so the blast radius is visible before
`ENTER`. Braking (STOP or STOP + TELL) also drops every session **in scope**
out of extreme mode, not only the ones it interrupted - an idle session left
armed is exactly the one that would push its own prompt next, so it loses
extreme too. TELL alone never touches arm state.

**`!` does not touch [timers](#session-timers).** A timer keeps firing on its
own schedule right through a STOP - a `now`-mode timer can re-inject seconds
after the `ESC` lands. `p` is what freezes those: it pauses relay's hands
entirely, including timer fires, until you press it again. `!` is a brake on
extreme and a channel to talk to sessions; it is not a way to silence a
timer.

### Parked work (`i`)

While a session works, you'll have a thought about what it (or another
session) should do next. You have three options. Type it into the session -
it lands in that session's context and pulls it toward work that wasn't the
point of the turn, so it drifts. Say nothing - the thought is gone by the time
the turn ends. Or park it: `i` on the selected row opens a DOS-style modal,
you type one line, `ENTER` parks it. Nothing was ever sent to the session, so
the capture costs it **zero context**.

For example: `bff-worker` is mid-refactor and you notice the retry backoff on
an unrelated service needs tuning. Press `i`, type "tune retry backoff on
inject", `ENTER`. `bff-worker` never sees the line and keeps refactoring
uninterrupted. Later, either some session claims it unprompted with
`relay next`, or you hand it to one directly: select that session's row in the
roster, press `b` to open the parked overlay, move to the item, `ENTER` -
it's assigned and that session gets a wake-up naming the task.

`TAB` inside the capture modal toggles SESSION (owned by that tab's swarm
name) vs DIR (owned by nobody, directory-wide) scope; an unregistered tab is
DIR-only - there's no swarm name to hand it to. The modal also lists what's
already parked in that directory - the five oldest, not the most recent, so
it won't catch a duplicate of something you just parked. `b` opens the parked
overlay instead: every item parked in the selected row's directory, oldest
first, uncapped - `←→` widens the scope to every directory and back, so
nothing parked elsewhere reads as lost. `↑↓` moves the cursor, `e` retitles
the highlighted item in place (fix a typo without losing the context stamp it
was captured with), `d` twice drops it - the first press arms and says which
item it would destroy, any other key cancels, and it disarms itself after five
seconds - and `i` from inside jumps straight to capture.

A session picks up its own parked work with `relay next` (claims the oldest
item it owns, then the oldest unowned one - never another session's) or
browses without claiming via `relay parked`. **Relay never pushes a parked
item into a session.** Extreme mode already knows how to inject a prompt into
an idle tab, and deliberately does not do that here: the item was parked
because you didn't want it worked on yet, and auto-draining the backlog would
turn "don't forget this" into "drift, unattended" - the failure this feature
exists to prevent, not enable. The only nudges toward a parked item are
visibility: the header count, each tab's status-bar badge, and the `i` / `b`
overlays themselves. Full protocol: `relay help parked`.

It runs the other way too: a session that notices a follow-up mid-task can
shelve it with `relay task add "<line>" --park` instead of drifting into it or
dropping it silently. Those land unowned in that directory, for whoever runs
`relay next` there next.

### Token usage (`CTX`)

The **`CTX` column** shows how full each session's context is, so you can see
which one is about to compact without switching to its tab. The preview pane
breaks the number down for the selected session:

```
TOKENS  62% of context · 124k/200k
        out 48.2k · in 3.1k · cached 1.2M
        41 turns · claude-opus-5
```

The percentage turns amber at 75% and red at 90%. Neither is a hard limit -
Claude Code compacts on its own - it's the point past which you may want to let
a session finish a thought rather than start one.

**Where the numbers come from.** Relay doesn't talk to Claude Code; it watches
iTerm2 tabs. But a Claude Code process exports `CLAUDE_CODE_SESSION_ID` into its
own environment, and `relay join` runs *inside* that process - so a session
hands relay the exact id of its own transcript, which relay then reads from
`~/.claude/projects/`. It's an exact pointer, not a guess.

**No registration needed.** Claude Code writes `~/.claude/sessions/<pid>.json`
for every running session, so relay walks up from the tab's foreground job to
the `claude` process and reads the session id straight off it. Unregistered
tabs report usage exactly like registered ones.

The walk matters: iTerm2 reports the *foreground job's* pid, which for a Claude
tab is often a descendant - an MCP server, a running Bash tool. On a live window
iTerm2 reported `92157` (`chrome-devtools-mcp`), whose grandparent `92030` was
the actual `claude`.

This route is preferred over the id `relay join` stores, because the stored one
goes stale the moment a tab restarts Claude - the DB would keep naming the
previous run's transcript and happily show its numbers forever. The stored id
survives as the fallback for when `~/.claude/sessions` is unavailable.

Relay deliberately does *not* fall back to guessing from the directory: sibling
tabs in one directory are the normal case here, they'd both resolve to the same
transcript, and a plausible wrong number is worse than no number on a panel
whose job is telling you the truth. A blank `CTX` almost always means Claude
isn't running in that tab.

**`cached` is reported separately and never folded into a total.** Cache reads
are repeat billing on the same prompt - they run to millions of tokens while
real input stays in the hundreds - so a single "total tokens" figure built from
them would measure nothing. `ctx` is a *level* (the last turn's prompt, which is
what a compaction resets); `out`, `in` and `cached` are cumulative.

### When relay can't read a session (`⚑ CANNOT READ`)

Relay reads Claude Code's on-screen chrome to tell working from idle and to
spot permission prompts. That chrome can change without notice - it has once
already - and every "I don't recognise this" path in relay fails safe to
*"nothing actionable"*. Per screen that's correct. Across a fleet it's a trap:
if the chrome changes shape, **every** session reads as a calm idle tab and the
panel reports quiet while relay sees nothing.

So relay watches whether it can still read at all. When a tab running Claude
shows none of the chrome relay depends on for ~15 seconds, the header says so:

```
⚑ CANNOT READ 2: api-worker, bff-worker · RELAY · SESSION CONTROL · 6 units …
```

It's advisory - it never changes a decision, and a blind session still fails
safe exactly as before. The point is that *"I can't read this"* stops being
indistinguishable from *"nothing is happening"*. It sits ahead of every other
count because those counts are derived from screens relay may no longer
understand.

A blank or starting-up tab is never reported: there's nothing to recognise and
nothing to be wrong about.

If you see it, relay's screen parsing needs updating - capture the frame into
`iterm/fixtures/screens/` and `python3 iterm/test_state.py` will fail on it
until the classifier can read it again.

### `relay review` - a verdict on relay's own judgment

`relay recap` says what relay *did*. `relay review` asks whether it was right
to, and it's the only place that separates two things the audit log records
identically:

```
relay review (last 7 days) - what relay decided, and on whose authority

  covering 2026-08-17 -> 2026-08-23

  approvals: 2363  (2136 cleared by the safety gate, 135 approved over it, 92 unverified)
  escalated to you: 240 · delivered 1 · extreme pushes 5
    186  a session asked you something
     41  the gate refused a command
     13  the gate could not read the screen
  9.6% of approvals did NOT come from the safety gate reading the command (227 of 2363)

  the gate said DANGEROUS and the arm level approved anyway (135):
    pkill/killall 31x · ssh 30x · psql 29x · curl -X 28x · rm -rf 27x · git push --force 4x

  the gate said DANGEROUS and it STOOD (41):
    ssh 12x · kubectl delete 9x · terraform 8x · rm -rf 7x · (other) 5x

  approved WITHOUT the gate being able to read the command (92):
    (unreadable) 92x

  by session: DRAGEN 94 · GLASS 26 · RELAY REWORK 22
```

Both of those land in the log as `auto-approved`, and they mean opposite
things:

- **cleared by the safety gate** - relay read the command and `danger.sh` said
  it was fine.
- **approved over it** - the gate said *dangerous* and the arm level
  (`wild`/`insane`/`extreme`) approved anyway. That's arming working as
  designed. It's also the only answer to *"what did I actually authorise when I
  armed that tab?"*
- **unverified** - approved without the gate being able to read the command at
  all (an off-screen heredoc header, an unparseable frame). Not overruled;
  unexamined - a different risk.

Overrides are grouped by the risky verb rather than the exact command, because
almost every command is a one-off: a real log produces 130+ buckets of "1x" and
answers nothing, while `ssh 30x · psql 29x` tells you what you've been waving
through. The rate is shown alongside the count on purpose - 135 reads as
alarming until you know it's 9.6% of 2,363, and reads as complacent if you only
ever see the percentage.

Escalations get the same treatment, and for the same reason. "Relay stopped
and asked you" covers a session asking a **question** (no command in sight),
the gate **refusing** a command, and the gate being unable to **read** the
screen - three different facts about how well the gate is working. A single
total reads as "the gate refused N commands", and an operator acting on that
reading goes off to loosen a gate that may never have fired. If nothing was
ever refused, the report says that in as many words.

The window is stated because `--all` cannot mean all time: the audit log is
pruned to its retention (7 days by default) at every TUI start, so the report
names the range it actually saw rather than implying history the log does not
hold. The rate carries its denominator for the same reason - 6.2% of 16
approvals is one `ssh`.

Nobody's being scolded: arming a tab `insane` is a deliberate choice. This is
where its consequences are visible once, instead of buried across thousands of
scrollback lines nobody re-reads. `--all` widens from 7 days to all time.

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
| `!` | **Intervene**: stop running sessions and/or broadcast to them (see [Intervene](#intervene-)) |
| `i` | **Park an idea** against the selected row (see [Parked work](#parked-work-i)) |
| `b` | **Parked overlay**: this directory's pile, oldest first, `←→` widens to every directory - `e` retitles, `d` twice drops, `ENTER` hands one to a session (see [Parked work](#parked-work-i)) |
| `,` | Open the **settings editor** (see below) |
| `n` | Go to (focus) the real iTerm2 tab for the selected session |
| `x` | Hide / show the selected session |
| `v` | **Audit view**: the selected session's record of unattended decisions (approvals, escalations, deliveries) in the feed pane; `v` again returns to the live feed |
| `t` | **Timers overlay**: the selected session's timers (see [Session timers](#session-timers)); `t` again or `esc` closes it |
| `m` | **Mascot widget**: open / close the floating desktop creature (see [The desktop widget](#the-desktop-widget)). Inert while an overlay is open, where `m` belongs to that overlay |
| `c` | **Caffeinate**: release the assertion so the Mac may sleep, or take it back. A release you make by hand is **sticky** - a session waking at 3am won't undo it - where an automatic one (`[power] release_after`) re-acquires the moment anything starts working. The header shows the countdown while it runs, and says so once released. Inert behind an open overlay, like `m` |
| `?` | Help overlay: key map + arm-level cheat sheet |
| `:` | Open the **command line**: type any capability by name (see [Commands](#commands-)) |
| `TAB` | Toggle the **swarm view** (kanban + discussions + PRs + messages) |
| `R` `R` | **Press twice:** restore dead workers (respawn in their workdir) |
| `W` `W` | **Press twice:** wipe dead sessions' work (delete). Guarded by the double-press |
| `Z` `Z` | **Press twice:** ZAP the whole project - all tasks, sessions and messages (`relay wipe --project <p> --all`). Refuses to guess when several projects exist |
| `E` `E` | **Press twice:** arm EXTREME on an INSANE session - configure the push prompt (double-press to confirm). Requires session already in INSANE mode |
| `q` | Quit (tears down the iTerm2 connection, releases `caffeinate` if it is still held - `c` releases it without quitting). Instant when idle; when sessions are armed or swarm work is live (queued messages, `doing` tasks) it asks for a **second `q`** within 5s - same confirm pattern as `R`/`W`, because quitting stops auto-approval and delivery |

`R` and `W` only act when a worker's tab has closed while it still owned tasks;
the panel shows a red hint and the count when that happens. The double-press is
the confirm - the first press arms it (auto-cancels after 5s), the second fires.

Hidden sessions don't vanish - they drop to a dimmed section at the bottom of the
list, and the cursor skips the divider as you navigate, so you fly between your
kept sessions while still being able to see and un-hide (`x` again) the rest. The
**live feed** pane below the list pulls the selected session's current screen the
moment you land on it (and updates as that session prints), so you see the live
prompt before you answer it.

The **SESSION** column is each session's name: the iTerm2 tab/session name you've
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

### Commands (`:`)

Press `:` to open a command line at the bottom of the panel. `TAB` completes
the name you've typed so far, `ESC` cancels, and `ENTER` submits.

Every capability in the panel - every key in the table above, plus a few
that never had a key - is a named command, and the key bar, the `:`
completion list, and the `?` overlay are all generated from one table
(`iterm/commands.py`). That's the point: two keys (`w` workspaces, `S` save
layout) once shipped working and invisible, because the key bar and the `?`
overlay were two hand-maintained lists, and adding a key to `BINDINGS`
touched neither. Now there is one list; a capability that isn't in it can't
be bound, completed, or shown, and one that is in it shows up everywhere by
construction.

Keys did not change - everything that had a key still has it. The key bar
only shows eight of those entries, the ones used dozens of times an hour
(move, arm, go to the tab, swarm view, pause, quit, send a digit); every
other command kept its key, it simply left the one-line bar, and is still
listed by key and by name (`:name`) in the `?` overlay.

A command that acts on a session (`:arm`, `:shadow`, `:hide`, `:park`,
`:extreme`) defaults to the selected row, same as its key would. Name a
session explicitly to target a different one: `:arm w1` moves the cursor to
the session registered as `w1` and arms that one, leaving whatever was
selected untouched.

Four commands are destructive and refuse to run without a trailing `!`:
`:restore`, `:wipe`, `:zap`, `:extreme`. `:wipe` alone prints what it would
do and stops there; `:wipe!` clears that gate and falls through to the same
arm-then-confirm safety its key (`W`) already uses - so from an unarmed
state the first `:wipe!` arms it, exactly like a first `W` press, and it
takes a second confirming `:wipe!` (or a `W` press) inside the countdown to
actually delete.

`:ws`, `:doctor`, and `:recap` don't act inside the panel - they shell out
to the `relay` CLI and print its output into the log pane. That happens in
the background: the command's echo line appears immediately, but the
roster keeps running while the subprocess is still going, and the result
lands whenever it finishes, tagged with an invocation number (`[1]`, `[2]`,
...) so two overlapping commands can't be attributed to each other.

Not everything relay can do is exposed here. Sixteen worker-protocol verbs
- `register`, `join`, `send`, `status`, `inbox`, `next`, `reply`, `ask`,
`say`, `agree`, `close`, `discuss`, `thread`, `task`, `timer`, `pr` - have
no `:` command at all: a Claude session runs those about itself as a
participant in a swarm, and running `register` or `join` from relay's own
panel would wrongly enroll relay's own tab as a session in the swarm it's
supervising. (`:help` does exist, but it's the TUI's own help overlay - it
shares a name with, and is otherwise unrelated to, the worker protocol's
`help` verb.)

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

The check compares your branch against the branch it tracks, so a checkout
with **no upstream** cannot be compared at all - the state where `git pull`
answers *"There is no tracking information for the current branch"*. Relay
refuses to guess and says which command fixes it, and `relay doctor` reports
the checkout's tracking state on every run:

```
  update: ✓ tracking origin/main
  update: ✓ tracking origin/main (3 behind as of the last fetch - 'relay update' will fast-forward)
  update: ✗ tracking origin/main, but DIVERGED: 1 ahead, 4 behind (as of the last fetch)
      'relay update' cannot fast-forward past your own commits. In ~/Work/relay:
      git log --oneline origin/main..HEAD   # what is local-only
      git pull --rebase                     # keep them, replayed on top
  update: ✗ the current branch is not tracking a remote branch, so relay
            cannot tell what is newer - in ~/Work/relay: git checkout main
```

Tracking a branch is not the same as being able to update: `relay update`
merges `--ff-only`, so a checkout carrying commits of its own cannot move at
all. Doctor reads the last-fetched state (it never goes to the network - a
health command that waits on a socket is one people stop running), so the
counts can be stale, and the question they answer is not.

That line exists because the failure is otherwise invisible: the daily auto
check skips in silence by design, so a machine in this state simply stops
updating and never says so.

**A clone made before 2026-08-22 cannot be pulled into.** This repo's history
was rewritten that day to drop an internal `docs/` directory from every
commit, so every commit after 2026-06-16 has a new hash. An older clone shares
only the first six commits with `origin/main` and `git pull` reports divergent
branches - correctly, and no reconciliation strategy will fix it. Check with:

```bash
git ls-files docs | head -1     # prints a path -> you are on the old history
```

If it does, save anything local first (`docs/` is tracked in the old history,
so a hard reset DELETES it), then adopt the published history:

```bash
cp -r docs ~/relay-docs-backup            # only if that directory exists
git fetch origin && git reset --hard origin/main
cp -r ~/relay-docs-backup/. docs          # now gitignored, stays local
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

### Telling sessions to work together

Point a session at relay and it self-onboards - no skill required, and no name
required either, because the CLI teaches the protocol itself:

    use relay to talk to the other sessions.

That is a complete instruction. `relay join` with no arguments registers the
session under a name derived from its working directory, shows it who else is
here, hands it anything already queued, and prints the rules it is expected to
follow: keep your status fresh (it is your heartbeat), reply to whoever
messaged you, never end a turn silent with a task still `doing`, and escalate
rather than guess. Naming is still yours if you want it - `relay join
api-worker` renames in place, carrying the session's messages and tasks along.

`relay help swarm` prints the same protocol without registering anything, for
reading first. Enrolment stays an act of the session itself: relay will not
enrol a tab it merely watches, because an enrolled session is one any local
process can send text to. What changed is that the act no longer has to be a
separate command carrying a name you invented.

#### Getting two or three sessions to agree on something

When a decision needs more than one session's judgement, you should not be the
one carrying messages between tabs. Open a discussion from any session:

    relay discuss bff-worker web-worker "one shared DB or one per service?"

Every participant sees every post. They are woken with a pointer
(`[relay discussion #7] 2 new post(s) from api - read them first: relay thread
7`) rather than the contents, and `relay thread 7` gives them the transcript,
who has settled, and what they can do next - as ordinary command output, so it
is never squeezed onto one injected line.

Reading first is the one thing relay does not leave to the sessions: `say`,
`agree` and `close` are refused while a participant has posts waiting for it -
which relay knows exactly, they are its own undelivered rows - and the refusal
prints those posts and marks them read, so re-running the command goes
straight through. It costs a bash call, not a turn. That is enforcing a fact,
not adjudicating a conversation: what a session does having read them stays
its call.

`relay agree <id> "<the position>"` records that a session is settled, and on
what - the position text is required, so "I agree" with no content is not
expressible. Posting again retracts it: a session still talking is not settled.

**The decision belongs to the sessions having it, not to relay.** Relay
carries the conversation and stays out of the outcome:

- it marks a discussion settled on exactly one condition - every participant
  posted `agree` - which is reading what they did, not judging it;
- any other ending is declared by an agent with `relay close <id> "<how it
  ended>"`, including "we agreed to disagree";
- if settling it genuinely needs you, **the agents decide that** and say so
  with `relay send --human`. Relay never makes that call for them.

`--rounds` (default 3) is a suggested per-participant budget, **not a limit**.
Relay tells a session when it goes past it and what another post costs, then
gets out of the way - silencing an agent mid-argument would be relay deciding
the conversation is over. The cost is real, though: N participants times R
rounds is N times R full Claude turns, spent while you are away. If a
discussion runs away, it is visible in the DISCUSSIONS pane and you can stop
it from the panel - the operator has the brakes, relay does not.

You are notified when a discussion settles, carrying the outcome rather than
the transcript. That notice is information, not a request.

For a single question to a single session, `relay ask <name> "<question>"` is
lighter - it blocks and hands back the answer inside the asking session's
current turn, so a question costs no turn boundary at all.

A session binds its identity from `$ITERM_SESSION_ID` (iTerm2 sets this
automatically), so every verb below resolves "me" without you passing an id:

```
relay join [<name>] [--role worker|coordinator] [--project <p>]
    START HERE. Registers this session AND prints, in one go: who else is
    in the swarm, anything already queued for you, and the protocol you are
    expected to follow. The name is OPTIONAL - with none, relay derives one
    from the working directory, so "use relay to talk to the other sessions"
    is a complete instruction. Passing a name later renames in place,
    carrying messages and tasks along. Safe to re-run. `relay register` is
    the same binding without the teaching.

relay who
    Who else is here: names, roles, status, last seen. Read-only.

relay help swarm | relay help pr | relay help discuss
    The protocol text alone, registering nothing.

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

relay send --pr <owner/name>#<n> "<body>" [--kind <k>]
    Route a message to whichever session claimed that PR. Exit 3 =
    unclaimed (nobody ran `relay pr claim`). Exit 4 = the owner session is
    gone (closed, or its name was rebound to a different tab). Relay never
    guesses at a substitute owner - escalate with --human on 3 or 4 instead.

relay send --human "<body>"
    Escalate to the operator: pings them (sound + notification) when the
    relay TUI is running, and shows in the swarm feed. Never injected into
    any session.

relay reply ["<body>" | <msg-id> "<body>"] [--kind <k>]
    Answer whoever wrote to you, without retyping their name. With no id it
    answers the last message received; if the last delivery was a batch of
    several it refuses and lists the ids. Records a correlation link.

relay ask <name> "<question>" [--wait <seconds>]
    Ask one session and BLOCK until it answers, printing the answer as this
    command's output - so the answer lands inside the asking session's
    current turn instead of costing it a turn boundary. Default 120s, max
    540s. On timeout the question stays queued and degrades into an ordinary
    message.

relay discuss <name> [<name>...] "<the question>" [--rounds N]
relay say <id> "<your view>"
relay agree <id> "<the position>"
relay close <id> "<how it ended>"
relay thread <id>
    Get two or more sessions to SETTLE something without you carrying
    messages between tabs. `discuss` opens a thread (topic LAST) where every
    participant sees every post; they are woken with a pointer and read it
    with `relay thread`. N (default 3) is a suggested post budget, not a
    limit - relay reports the cost of going over and never refuses a post
    for being long. `agree` does not consume budget, and posting after
    agreeing retracts it. The one refusal: say/agree/close are blocked while
    posts are waiting for you, and the refusal prints them so the retry
    costs a bash call, not a turn.
    Relay closes a thread on ONE condition, that everyone agreed, and pings
    you with the position - not the transcript. Every other ending is the
    agents': `relay close`. Relay never declares a discussion failed and
    never hands them your decision. Rules: relay help discuss

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

relay pr set <owner/name>#<n> --state created|review|changes|approved|merged|closed
             [--title <t>] [--branch <b>] [--project <p>]
    Push a PR's CURRENT state into relay. Relay never calls gh or looks at
    GitHub - it stores what you tell it, and everything that displays a
    state also displays how old that report is. Run it for every PR your
    sweep sees, claimed or not.

relay pr claim <owner/name>#<n> [--task <id>] [--branch <b>]
    Record that THIS session opened this PR - run it right after
    `gh pr create`. The only thing that makes "which session owns this PR"
    answerable later.

relay pr list [--project <p>] [--mine] [--days <n>]
    PRs in stable order (repo, then number) with state, age of that state,
    owner, task, and an UNCLAIMED or GONE marker.

relay spawn --name <name> "<prompt>" [--project <p>] [--dir <path>]
            [--role worker|coordinator] [--arm off|safe|wild|insane]
            [--worktree] [--share]
    Open a new iTerm2 tab running claude, pre-registered under <name>.
    --worktree (with --dir <repo>): spawn in a fresh git worktree of that
    repo instead of the repo itself.
    Refused if no arm level was given (an unarmed worker stalls at its first
    permission prompt with nobody at that tab), or if a live worker already
    occupies that --dir (two sessions in one working copy overwrite each
    other). Say --arm off, --worktree or --share to mean it.

relay timer add --key <slug> --every <1-90> --times <1-50> --say "<text>"
    Register a timer on YOUR OWN tab: <say> is typed in and submitted every
    <every> minutes, but only while you are idle at a ready prompt. Needs no
    `relay register` - timers bind to the tab, not to a swarm name. --key is
    stable: re-running with the same key updates that timer instead of adding
    a second. --times is a mandatory fire cap (no unlimited); an exhausted
    timer cannot raise its own cap by re-registering - only the operator can
    restart it. Mode is always idle (there is no --mode flag). Max 5
    self-registered timers per tab. See "Self-scheduling from inside a
    session" below.

relay timer list
relay timer rm --key <slug> | --id <n>
    Your own timers only (id, key, interval, on/off, fires left, next fire,
    payload), and removing one of them.
```

Identity-free verbs (`relay task list`, `relay msgs`) work anywhere. The
identity-bound verbs (`register`, `status`, `send`, `inbox`, `timer`) resolve
"me" from `$ITERM_SESSION_ID`, so they need an iTerm2 session to run. Delivery
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

### Burn: working, unattended, going nowhere

Staleness catches the session that went **quiet**. The expensive failure is the
one that stays loud: a session retrying the same failing test for four hours
reads `working` on every tick, so the panel reports a calm fleet while it
spends. Every other signal relay has is a snapshot of what a session is
*doing*; none measures what it *achieved*.

So relay watches the **git working tree** in each tab's directory. A session
that has been working for `[burn] window` minutes (default 15), while you were
not in its tab, with turns completing and the tree never moving, gets `◈ BURN`
in STATUS, a `n burning` count in the header, and an evidence line in the
preview:

    ◈ BURN  22m unchanged, 18 turns, 85.2k out

**Informational only.** Relay never acts on it - no interrupt, no message, no
notification. `▲ STALE` outranks it in the cell, because a session relay has
lost is worse news than one merely going nowhere.

Tokens are shown but are **not** the trigger. A retry loop emits short tool
calls over a huge cached context, so it can spend heavily while producing less
output than a productive session - calibrated against real transcripts, an
output-token threshold fired on ordinary work and missed the loop it was for.
Time is the signal; the turn count is the floor that separates a loop from one
long thinking turn.

Relay stays quiet where it cannot be sure, and the preview says which:

- **the tab you have selected** - your clock is held at zero while you are in
  it, because if you are watching the loop you do not need to be told about it;
- **a directory with more than one live session** - the normal case for sibling
  tabs, and there the tree cannot say which session moved it;
- **no readable git tree, or no transcript yet** - nothing to measure.

Set `[burn] window = 0` to switch it off.

### TAB: the swarm view

`TAB` toggles a second, full-width view:

- a **FLEET line** on top - unit counts by state (busy / blocked / idle),
  armed counts by mode, stale count, queued messages;
- under it, from 8 units up, a **fleet map**: one cell per unit in offset
  honeycomb rows, filled (`●`) when the unit is armed or working and hollow
  (`○`) when it is idle, colored by urgency (red stale, yellow blocked, green
  working). No names - the roster below is where those live; the map is for
  peripheral vision, so one hot cell breaks the pattern instead of hiding in
  a column. A smaller fleet gets no map, because the roster already answers
  it at a glance;
- the roster with a per-worker **heartbeat** (`↻ 12s` since its screen last
  moved; a stale row renders red with `⧗`);
- a kanban board of tasks by state (TODO / DOING / BLOCKED / DONE) and epic
  **progress bars** (`▰▰▰▰▱▱▱▱  4/8`);
- an **INTERACTIONS** map - who talks to whom: per-pair sent/received
  counts, last message kind and age, `‼` when the pair's last word was
  `blocked` or `escalation`;
- a **DISCUSSIONS** pane: open discussions with how many participants have
  settled and how old they are, and any that have reached a verdict
  duplicated into an attention strip on top (same rule as the PR pane - the
  main list never reorders);
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

Spawn refuses two setups outright rather than teaching against them, because
both are conditions relay already stores rather than judgement calls: a worker
with **no arm level** (it stops at its first permission prompt with nobody at
that tab to clear it - pass `--arm wild`, set `[swarm] spawn_arm`, or say
`--arm off` explicitly if you will sit there), and a `--dir` a **live worker
already occupies** (pass `--worktree`, or `--share` if the new session will
only read there). A coordinator sitting in the repo it delegates from is not a
collision; `relay who` marks peers that share your working copy, and `relay
doctor` names any two workers that already do.

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
    and every message it sent or received (queued or delivered - posts
    in still-open discussions excepted). It destroys exactly the workdir
    context that restore needs, so if you're not sure which one you
    want, run `relay restore` first - `relay clean` is for orphans you've
    decided are not worth reviving.

relay wipe [names...] [--project <p>] [--all] [--dry-run] [--yes]
    The delete-counterpart to clean: instead of resetting a closed
    session's non-done tasks to todo, it DELETES those tasks outright
    (any state, including done), then deletes the session row and every
    message it sent or received - queued or delivered, so no ghost mail
    or stale transcript survives it (posts in still-open discussions are
    the one exception, kept until the thread closes). Same candidate
    set as clean - no names =
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

    Like restore and clean, it always prints a WIPE PLAN first (task,
    session and message counts, or the project totals for --all), then asks to
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

### Workspaces

A workspace is a **named set of tabs** - directory plus command each - saved
to `~/.relay/workspaces.toml` (override the path with `RELAY_WORKSPACES`) and
brought back with one command. **A workspace is not a substitute for `relay
restore`, and restore is not a substitute for a workspace:** restore resolves
orphaned *task ownership* - it revives closed sessions that still own
unfinished tasks - while a workspace reproduces *a layout*, whether or not
anything in it was ever a tracked task. They answer different questions and
both stay.

Relay already uses the word "workspace" for something else: the live groups
of tabs that share a launch directory, drawn as the `┎┃┖` rail in the session
table (`iterm/workspaces.py`). The two are meant to be read together, not as
a naming collision - the rail is the **live** view of the idea, and
`~/.relay/workspaces.toml` is its **saved** form. One honest wart: a saved
workspace's tabs each carry their own `dir`, so a single saved workspace can
span several directories, which no live rail group can represent.

A tab is a directory plus a command - there is no `kind` key distinguishing a
plain shell from a Claude session. Behavior falls out of which keys are set:

| shape | what happens |
|---|---|
| `cmd` alone | a plain tab - not registered, not armed |
| `cmd` + `arm` | registered under `name` and pre-armed before `cmd` runs; no prompt injected |
| `cmd` + `arm` + `prompt` | the existing full swarm-worker path (same as `relay spawn`) |

Per-tab keys, all optional except `name`:

| key | meaning | default |
|---|---|---|
| `name` | tab title, and the relay session name once `arm` is set | required |
| `dir` | working directory | `~` |
| `cmd` | command run once the shell is up | none |
| `arm` | `safe` / `wild` / `insane` - registers and pre-arms the tab | unset |
| `prompt` | mission string; presence routes through the same spawn path as `relay spawn` | unset |
| `role` | `worker` / `coordinator` | `worker` |
| `project` | swarm project tag | `""` |
| `window` | groups tabs into separate windows (tabs sharing a number open in the same window) | `1` |
| `panes` | extra panes split into the tab, each `{ cmd, dir, split }` (`split` is `v` side-by-side or `h` stacked) | `[]` |

An optional top-level `[settings]` table holds `target` (`new` or `current`,
default `new`) and `warmup` (seconds before commands are typed, default
`1.5`); `relay ws up --here` / `--new` override `target` for one call.

```
relay ws save <name> [--all] [--force] [--config <path>]
    Snapshot the current window (--all: every window) into the config file.
    Refuses to replace an existing workspace of the same name unless --force
    is passed.

    A running tab cannot report the command it was started with, so `cmd` is
    written empty and you fill it in by hand. Relay's own panel tab and any
    reserved name are skipped without comment; a tab with no name is dropped
    and the drop count is printed; a tab with split panes is saved as its
    first pane only, and that loss is reported too.

relay ws up <name> [--here] [--new] [--dry-run] [--yes] [--config <path>]
    Build a workspace. Always prints a plan first; without --yes it asks to
    confirm before opening anything, and --dry-run stops after the plan.
    --here puts the first window's tabs in the current window; any tab
    assigned `window = 2` or higher still opens its own new window. --new
    always opens new windows.

    Any tab whose name is already a live tab is skipped, not rebuilt - this
    is what makes `ws up` idempotent, and it stops a workspace from stealing
    a running session's name out from under it.

relay ws list [--config <path>]
    List every workspace defined in the config file.

relay ws rm <name> [--config <path>]
    Delete a workspace from the config file.
```

In the TUI, `w` opens an overlay listing the workspaces that are defined
(`relay ws up <name>` opens one); `S` saves the current window as a new
workspace, prompting for a name.

> `~/.relay/workspaces.toml` can arm sessions. `arm` on a tab makes relay
> register and pre-arm it, which is the same local-trust boundary as
> `db.set_arm_request` and `queue_message`: any process that can write your
> relay files can request arming. Treat it like the rest of `~/.relay`.

### Pull requests: who owns what

Relay never calls `gh`. A session tells it what it sees, and relay answers one
question in return: **which session opened this PR?**

A worker claims its PR the moment it opens one:

    relay pr claim acme/api#482 --task 14

A PR-sweep session pushes what GitHub currently says, then routes feedback
straight to whoever wrote the code:

    relay pr set acme/api#482 --state changes
    relay send --pr acme/api#482 "changes requested: tighten the rate limit test"

That message is typed into the claiming session when it goes idle. If nobody
claimed the PR (exit 3), or the claiming session is closed or its name has been
rebound to a different tab (exit 4), relay refuses to guess and you decide:

    relay send --human "acme/bff#77 has changes requested and no owner"

which pings you immediately and is never injected into any session. `TAB` shows
the PR pane: what needs work on top, then every PR in stable order, each with
the age of the last report beside its state - relay only knows what it was
last told, and the pane never pretends otherwise.

Retention is `RELAY_PR_RETENTION_DAYS` (default 7). Merged and closed PRs age
out; open ones never do.

### Skills

`skills/relay-worker` and `skills/relay-coordinator` are the protocol layer:
what a worker does on start (register, check `relay inbox`, split an
assigned epic into subtasks, keep `relay status` fresh, message the
coordinator when done or blocked) and what a coordinator does (write specs,
create epics with `--owner` and `--spec`, spawn or address named workers,
monitor via `relay task list`). Both skills share one CLI verb reference,
[`skills/relay-cli-reference.md`](skills/relay-cli-reference.md), copied
above.

[`skills/relay-self-scheduling`](skills/relay-self-scheduling/SKILL.md) and
[`skills/relay-parked-work`](skills/relay-parked-work/SKILL.md) are the other
two, and neither is swarm-scoped - both load in a plain unregistered tab.
`relay-self-scheduling` loads when a tab is told to own something on a
repeating schedule ("you're responsible for PRs"); it deliberately carries no
flag syntax - the CLI's own errors teach that, whether or not a skill loaded.
What it carries is the judgment the CLI cannot check: when a standing timer is
the wrong tool, how to write a payload that still makes sense after the
session has been compacted three times, and what intervals and caps are sane.
`relay-parked-work` loads on "pick the next one from relay" / "what's parked"
/ "anything queued for me" and carries the same kind of judgment: a parked
item is a seed, not a spec, and claiming one is the operator's call, not a
license to auto-drain the backlog (see [Parked work](#parked-work-i)).

`relay help parked` is the canonical protocol text either way - the skill
points at it rather than duplicating it, so a session with the skill
uninstalled still gets the rules by running the CLI.

`./install.sh` offers to symlink all four into `~/.claude/skills/` so they
version with the repo instead of drifting.

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
| `RELAY_NO_CAFFEINATE`        | unset                      | Set to `1` to never keep the Mac awake (see `[power] release_after` to release it on an idle fleet instead) |
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
| `RELAY_WORKSPACES`           | `~/.relay/workspaces.toml` | Named-workspace config file (see [Workspaces](#workspaces)) |

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
enabled = true         ; master mute - false silences all four without losing
                       ; your picks; toggle it live in the settings editor (,)
alert = /System/Library/Sounds/Sosumi.aiff
done  = /System/Library/Sounds/Glass.aiff

[swarm]
stale_minutes   = 10
notify_cooldown = 30
spawn_arm       = off  ; arm level for spawned workers: off | safe | wild | insane
                       ; honored only at FIRST sight of a session (spawn's boot
                       ; window); a request appearing later for a known session
                       ; is refused and escalated (self-escalation guard)
respect_draft   = true ; default. Never type into a session whose input box
                       ; already holds a half-written message of yours: a
                       ; queued swarm message and a due timer both wait
                       ; instead (nothing is consumed - they retry the moment
                       ; the box is clear). false restores the old behaviour,
                       ; where the delivery appends to your sentence and
                       ; presses Enter. The extreme push refuses a draft
                       ; either way, whatever this is set to

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

[mascot]
name = crt             ; which creature watches the fleet - crt | invader | owl
                       ; | cat | core | beacon | ghost | crab | droid | bug
                       ; | skull | toaster | atom | moth | tank (default crt).
                       ; Purely cosmetic: every skin runs the SAME moods, tick
                       ; and colors. Cycle it live in the settings editor (,)

[layout]
preview = true         ; show the live-feed pane under the list (default true);
                       ; toggle live with f, or here in the settings editor

[power]
release_after = 0      ; minutes of a FULLY IDLE fleet (nothing in the working
                       ; state) before relay releases caffeinate. 0 = never,
                       ; the default, which keeps today's behaviour. Releasing
                       ; is not sleeping: relay stops preventing sleep and
                       ; hands the decision back to macOS, which knows from
                       ; real HID input whether you are at the machine - so a
                       ; 30 here cannot sleep a Mac you are sitting in front
                       ; of. A blocked session counts as idle (it is going
                       ; nowhere until a human arrives). Release by hand with
                       ; c, which is sticky where the timer's release is not

[burn]
window = 15            ; minutes with an UNCHANGED git tree, while working and
                       ; while you are not in the tab, before relay badges the
                       ; session ◈ BURN. 0 = off. On by default, because it
                       ; only draws a badge - it never acts. Tokens are shown
                       ; as evidence but are NOT the trigger: a retry loop
                       ; emits short tool calls over a huge cached context, so
                       ; it can spend heavily while producing less output than
                       ; a productive session
```

Deliberately not configurable here: bootstrap paths (`RELAY_DB`,
`RELAY_CONFIG`), session-scoped flags (`RELAY_NO_CAFFEINATE`,
`RELAY_NO_REACTOR`, `--dry-run`), the spawn boot delay, `lib/danger.sh`'s
rules (own home), and the title glyph/word vocabulary (it doubles as the
strip-parser - a configurable vocabulary would double the bug surface).

### Boot screen

Relay's startup is not instant - the iTerm2 handshake and the first sweep of
every session cost real time - so it fills that time with a full-screen POST
instead of a blank panel:

```
                    ██████╗ ███████╗██╗      █████╗ ██╗   ██╗
                    ██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝
                    ██████╔╝█████╗  ██║     ███████║ ╚████╔╝
                    ██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝
                    ██║  ██║███████╗███████╗██║  ██║   ██║
                    ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝
                         S E S S I O N   C O N T R O L

     ──────────────────────────────────────────────────────────
       Memory Test       : 262144K  OK
       Config            : phosphor · gate=default
       Safety Classifier : lib/danger.sh · preset=default
       Audit Log         : 1204 entries · pruned 3
       Event Seam        : ~/.relay/events.jsonl · 24 rows
       iTerm2 Link       : established · 0.42s
       Sessions          : 4 found · 2 armed

                          WELCOME, OPERATOR █
```

Every line except the memory test is a subsystem reporting its real state, so
a boot that stalls tells you **which** part stalled. It never outlives the
work: the screen dismisses as soon as the last line reports, and **any key
skips it**.

```ini
[boot]
enabled = true    ; false to go straight to the panel
style   = bios    ; bios | console | crt | minimal
```

Four styles over the same report - pick the one you want to look at every
morning:

| style | what it is |
|-------|-----------|
| `bios` | the full-screen POST above (default) |
| `console` | a top-anchored boot log: `[  OK  ]` / `[ WARN ]` / `[ FAIL ]` in one column, so a subsystem in trouble is the line your eye lands on |
| `crt` | the POST under a phosphor scanline, dot leaders instead of a colon |
| `minimal` | a wordmark, a progress bar, the subsystem it is waiting on and the last thing that reported - two lines, for people who want the diagnosis without the splash |

Whatever the style, the rules are the same: it names the subsystem it is
waiting on, it shows a value the moment that subsystem reports one, and it
never signs off early. A style that only decorates is what `enabled = false`
is for. Both keys are editable in the settings overlay (`,`). `RELAY_NO_BOOT=1` disables it
for a single run without touching config - which is how the test suite runs,
since a pilot pressing keys would spend its first press dismissing the screen.

Boot styles are a plug point: `iterm/boot.py` maps a name to a renderer, and
adding one is an entry in that dict plus a function in that file. Nothing in
`app.py`, `config.py` or `settings.py` learns the new name. The module is pure
- no Textual, no iTerm2, no clock, palette passed in - so frames are tested
directly at any terminal size, and deleting it leaves relay starting normally.

### Events

Relay appends everything it would notify you about to `~/.relay/events.jsonl`,
one JSON object per line, so something other than this Mac's speaker can react:

```json
{"v":1,"ts":1755640000.1,"kind":"gate.escalated","session":"api-worker",
 "session_id":"w0t1","title":"Relay - api-worker",
 "message":"DANGEROUS_COMMAND: terraform apply -auto-approve","data":{}}
```

Kinds: `gate.escalated`, `arm.changed`, `arm.refused`, `audit.failed`,
`session.stale`, `escalation.received`, `task.done`, `extreme.exhausted`.

Relay does not run hooks. It never executes anything from this seam - a session
relay supervises can write files, and relay must not run code `lib/danger.sh`
never saw. You get the same power from a process **you** started:

```sh
tail -f ~/.relay/events.jsonl | while read -r ev; do
  case "$ev" in *gate.escalated*) say "relay needs you" ;; esac
done
```

To get a ping off the desk without running anything, set a `post_url`:

```ini
[events]
file           = true
post_url       = https://ntfy.sh/your-topic
post_body      = minimal   ; minimal | full
retention_days = 7
```

A `post_url` must start with `http://` or `https://`. A `#` or `;` **preceded by
a space** starts an inline comment and truncates the value - `post_url =
https://ntfy.sh/t #note` parses as `https://ntfy.sh/t`. One inside the URL is
kept, so fragments and query strings are fine. `relay doctor` shows the host it
will POST to.

`post_body = minimal` (the default) sends only `v`, `ts`, `kind` and `session`:
you learn *which* session wants you, not what it wanted to run. `full` sends the
whole envelope, including command text, to whatever host you configured - opt in
knowingly. `post_url` is config-file-only; `relay doctor` shows whether one is
set.

### Sounds and the settings editor

Relay uses four distinct sounds so your ear can triage without looking, all set
in `[sounds]` (any can be set empty to silence just that one):

| Key | Fires on | Default |
| --- | -------- | ------- |
| `danger` | a session about to run a dangerous command | Basso |
| `alert` | needs a look (real question, stale session, error) | Sosumi |
| `message` | a swarm worker messaged / escalated to you | Tink |
| `done` | a task or epic completed | Glass |

To go quiet for a while, flip `enabled = false` (the first row of the SOUNDS
group in the editor) instead of blanking the four keys: it takes effect on the
running watcher immediately, the muted rows are tagged `(muted)`, your four
picks survive, and flipping it back restores them. `p` still auditions the
highlighted sound while muted - you asked to hear it.

Press **`,`** in the panel to open the **settings editor**: `↑`/`↓` move
between settings, `←`/`→` change the highlighted one, `p` auditions the
highlighted sound, and every change is saved to `~/.relay/config` as you go -
no separate save step. Sound changes apply immediately, as do the feed pane
and the **mascot** (hold `←`/`→` on APPEARANCE > mascot and the creature in the
banner changes under your hand); the rest take effect on the next relay start
(the editor tags those fields "restart to apply"). On
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
the bare name). Reads are always clean regardless - the SESSION column and swarm
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
🟢 RELAY:safe · 2 PARKED         2 items parked in this tab's directory
⬛ RELAY: panel                  relay's own tab (inert - relay never arms itself)
⚫ RELAY: off                    relay itself is not running
```

(The color comes from the emoji circle: iTerm2's status-bar API returns plain
text, so a colored glyph is how you get color-per-mode.)

The `N PARKED` suffix (see [Parked work](#parked-work-i)) appears on every tab
whose directory has parked items, armed or not - the badge is on every tab,
which is where the operator actually looks, and it is the whole nudge: relay
never pushes a parked item into a session.

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

### Session timers

A **timer** fires a payload string (a command, a nudge) into one session on a
repeating interval - cron-for-a-tab, without leaving the panel. Press `t` on a
selected session to open the **timers overlay**:

| Key | Action |
| --- | ------ |
| `a` | Add a timer |
| `enter` | Edit the highlighted timer's payload |
| `left` / `right` | Change the highlighted timer's interval (1-90 minutes) |
| `m` | Cycle the highlighted timer's mode: `idle` <-> `now` |
| `[` / `]` | Lower / raise the highlighted timer's **fire cap** (`0` = unlimited) |
| `space` | Toggle the highlighted timer on/off |
| `g` | Fire the highlighted timer now (still goes through the normal audited send) |
| `x` | Delete the highlighted timer |
| `r` | **Restore** a timer that needs it, or **restart** a `done` (capped) timer - same key, depends on the row's state |
| `esc` | Close the form, or the overlay if no form is open |

The highlighted row is marked with `▸` and bolded, so you always see which timer
your keys act on. Each row shows its interval, mode, on/off, **fire-cap
progress** (`3/10`, or `∞` for unlimited), and its countdown.

**Fire cap:** a timer stops after firing `max_fires` times (default **10**), so a
`now`-mode timer can't run forever by accident. `0` means unlimited. A capped
timer shows **done (cap reached)**; raise its cap with `]` to resume it. Only
real fires consume the cap - dry-run and "fire now" (`g`) do not.

The session's list row carries a **`⏲ TIMERS` column** showing the count of
active timers (or `?` when a timer needs restore). The next-fire countdown lives
in the feed-header summary and the `t` overlay, where it belongs to a specific
timer.

**`idle` vs `now`:** an `idle` timer only fires once the session is sitting at a
ready prompt - it waits rather than interrupting a running command (a "check in
when you're free" nudge). A `now` timer fires the moment it's due, regardless of
what the session is doing.

**Restore on start:** a timer is bound to one iTerm2 session id, and session ids
don't survive quitting iTerm2 or relay. So on launch, a saved timer whose
session isn't present yet - or whose binding has aged past `reconfirm_days` - is
marked **needs restore** instead of firing blind into a possibly different tab.
The session's row in the list shows `⏲?` when this happens; select it, open
`t`, and press `r` to re-bind the timer to that session and re-arm it.
`autostart = true` (below) skips this prompt entirely and re-activates saved
timers on start.

Firing is always **audited** the same way an auto-approval is - even "fire now"
(`g`) goes through the normal `_fire_timers` send path, never a raw keystroke
from the UI.

Note: a timer does not fire while its session is both hidden (quarantined)
and disarmed - unhide or arm the session to resume it. This is a side effect
of the poll loop's hidden+disarmed optimization, which skips reading a
hidden+disarmed session's screen entirely (nothing to check in on).

Config, in `~/.relay/config`:

```ini
[timers]
require_armed  = false   ; only fire on an armed session
autostart      = false   ; skip the restore prompt; activate saved timers on start
reconfirm_days = 7        ; re-confirm a timer binding older than this (0 = never)
```

`require_armed` and `reconfirm_days` take effect live, no restart needed.

### Self-scheduling from inside a session

A Claude session can register its own timer, from inside a plain `relay
timer add` call, no `relay register` needed - timers bind to the iTerm2 tab,
not to a swarm name. This is how a session takes standing responsibility for
something ("you own PR review, check every 20 minutes") without a human
opening the `t` overlay for it. The `relay-self-scheduling` skill carries the
judgment (should this be a timer at all, what interval is sane, write a
prompt file instead of a long payload); the CLI carries the mechanics:

```bash
relay timer add --key pr-duty --every 20 --times 10 \
  --say "Read .relay/prompts/pr-duty.md and do what it says."
```

Guards apply only on this path, because a session scheduling itself is
a different risk shape than an operator scheduling it from the panel:

- **Mode is always `idle`.** There is no `--mode` flag - a `now`-mode timer
  firing mid-turn would corrupt the session's own turn, so the option simply
  doesn't exist here.
- **The fire cap is mandatory**, clamped to 1-50 - unlike the panel's `0` for
  unlimited. An unattended session scheduling its own repeated injections
  needs a hard ceiling; when the cap runs out, re-running `add` does **not**
  revive it - the row is left exhausted with its `fire_count` untouched, and
  only an operator can restart it, from the `t` overlay (select it, press
  `r`).
- **`--key` upserts.** Re-running `add` with the same key updates that
  timer's interval/payload/cap in place instead of stacking a second one, so
  a session that re-registers every turn (a common failure mode without this)
  doesn't quietly multiply its own firings. The upsert never touches
  `enabled`/`active`, though - see below.
- **A per-session cap of 5 timers**, checked only when registering a
  brand-new key (an upsert of an existing key always goes through, even at
  the limit - otherwise a session at the cap could never update its own
  timer). A session that invents a new key every turn instead of upserting
  hits this quickly; `relay timer list` / `relay timer rm` free up a slot.
- **`--every` rejects junk instead of clamping it.** A typo like `--every
  60m` errors out naming the flag, rather than silently becoming a 1-minute
  timer - a silent clamp there would be 60x more aggressive than intended,
  on exactly the path this design treats as a token-bonfire risk.

A timer created this way carries the label `self:<key>`, visible in the `t`
overlay next to any operator-created rows. It is still an ordinary row in the
same table, so it is bound by the same restart behavior as every other
timer: relay does not resume firing anything on its own. On startup every
saved timer loads with `active = 0` (unless `autostart = true`) and needs the
operator to select it and press `r` to restore it - a self-registered timer
is no exception, and does not come back on its own after a relay restart.
Re-registering it in the meantime does not change that: the UPDATE path
never sets `enabled` or `active` - only a brand-new registration goes live
immediately. If the existing timer is off or pending restore, `relay timer
add` says so in its output, so the fact lands in the session's transcript
instead of staying silent.

### The desktop widget

relay's pitch is *walk away and trust it* - but walking away means the panel is
behind six windows, and the one thing relay exists to tell you is the thing you
can no longer see. The **widget** is the mascot on your desktop, above other
apps, so the fleet's state survives you leaving the terminal.

Press **`m`** to open or close it. `[widget] enabled` (settings editor, or
`~/.relay/config`) controls whether it opens *with* relay; it defaults to
`true`. The key works either way - it never depended on the setting.

**It is read-only, deliberately.** It renders what relay publishes and derives
nothing. There is no pause button and no arm toggle: a floating always-on-top
window that could pause your supervisor is an accident waiting to happen, and it
would undercut the "a physical click is un-spoofable" property the
[status-bar badge](#iterm2-status-bar-arm-badge) relies on. The one thing it
does do is *navigate* - clicking the **RELAY** wordmark brings you back to the
panel tab, which cannot arm, approve or inject anything.

| | |
| --- | --- |
| **Two sizes** | Double-click to switch between the creature and a compact strip showing just the sentence. It remembers the size you dragged it to, and the mode persists across launches. |
| **Resize** | Hover for a grip in the bottom-right. The mascot is a fixed character grid, so resizing scales the type - drag it big and the creature grows. |
| **Fullscreen** | It stays visible over fullscreen apps, which is precisely when you are away from the terminal. |
| **Offline** | If relay stops, the published state goes stale within 5s and the creature greys out and says so - the same contract as the status-bar badge's `⚫ RELAY off`. |

**How it talks to relay.** relay atomically writes `~/.relay/widget.json` once a
second (the mascot block, its mood and colour, and the fleet counts); the widget
polls it and treats anything older than 5s as "relay is off". One-way, no
socket, no handshake - the same publish/poll shape the status-bar provider
already uses. relay publishes whether or not it launched the widget, so a
widget started any other way still gets fed.

**Building it.** The widget is a Tauri app, so unlike the rest of relay it needs
compiling once:

```bash
cd widget/src-tauri && cargo build --release
```

`widget/src-tauri/target/` is gitignored (over 1GB), so a fresh clone must build
it - until then `m` prints exactly that command instead of doing nothing. This
is the one place relay needs a toolchain beyond `python3`.

`widget/mascots/` holds painted mascot art generated by
`widget/make-mascots.py` (fal.ai; the key is read from `~/.relay/fal.key`,
**outside the repo**, and `lib/secret_scan.sh` installs as a pre-commit hook
that refuses any commit containing credential-shaped content). The ASCII
creature remains the source of truth - the painted art is not wired into the
widget today.

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
  iterm/timers.py        # pure timer scheduling logic (due/firable/reconfirm), no iTerm2/sqlite imports
  iterm/swarm.py         # pure swarm logic: delivery text, staleness, rendering
  iterm/cli.py           # swarm CLI verbs (register, send, task, inbox, ...)
  iterm/spawn.py         # relay spawn: new iTerm2 tab + claude + pre-registration
  iterm/usage.py         # token usage: transcript lookup + incremental reads (no iTerm2/sqlite imports)
  iterm/statusbar.py     # status-bar badge: pure labels + published state / click queue
  iterm/statusbar_autolaunch.py  # always-on badge provider (symlinked into iTerm2 AutoLaunch)
  iterm/test_*.py        # gate/TUI/swarm suites, built from real captured prompts
  iterm/test_config.py   # config loader tests (temp files, precedence)
  iterm/test_titles.py   # render/strip round-trip tests
  iterm/test_db.py       # swarm schema + query tests (temp DB file)
  iterm/test_timers.py   # timer scheduling logic tests (due/firable/reconfirm)
  iterm/test_swarm.py    # delivery/staleness/rendering logic tests
  iterm/selftest.py      # relay selftest: live read check + fixture capture
  iterm/test_cli.py      # CLI verb tests against a temp DB file
  iterm/test_usage.py    # token usage tests against hand-written transcripts
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
