# Worktree spawn + typed messages - design

Date: 2026-07-19
Status: approved (brainstormed with Maciej; TUI visualization explicitly deferred
until these two ship)

Two swarm-layer features, inspired by a survey of peer orchestrators (Overstory's
typed SQLite mail bus, cmux/claude-squad worktree isolation, Metaswarm's
escalation rules):

1. `relay spawn --worktree` - conflict-free parallel workers in one repo via git
   worktrees.
2. Typed messages + broadcast - a `kind` on `relay send`, project-wide send, and
   an `escalation` kind that pings the human.

Guiding stance (Maciej's words): relay is a tech a session uses, not a place
that owns the work. Worktrees live next to the repo, never under `~/.relay`.
Relay never forces the layout - the manual `--dir` route stays first-class.

## 1. `relay spawn --worktree`

### CLI surface

```
relay spawn --name bff --project webshop --dir ~/work/webshop --worktree "<prompt>"
```

- `--worktree` is a flag, valid only together with `--dir`; `--dir` must point
  at a git repository (a `git -C <dir> rev-parse --git-dir` check). Violations
  are CLI errors before anything is created.
- Effect, in order:
  1. `git -C <dir> worktree add <parent>/<reponame>-<name> -b relay/<name>`
     (branch from the repo's current HEAD). Sibling path example:
     `~/work/webshop` + name `bff` -> `~/work/webshop-bff`, branch `relay/bff`.
  2. Spawn the worker with its workdir set to the worktree path (existing
     workdir recording, so `relay restore` works unchanged).
- Failure of the `git worktree add` (existing path, existing branch, dirty
  lock) aborts the spawn with git's stderr shown; nothing is registered.
- The session row records the source repo in a new `worktree_repo` column, so
  cleanup can tell a relay-created worktree from a plain workdir.

### Lifecycle

- `restore`: unchanged - respawns in the recorded workdir (the worktree).
- `clean`: unchanged - never touches git state.
- `wipe`: for a session with `worktree_repo` set, the WIPE PLAN additionally
  offers removal of the worktree and its `relay/<name>` branch, but only when
  `git status --porcelain` in the worktree is empty. A dirty worktree is kept
  and a warning names it (uncommitted work is never deleted). Removal runs
  `git worktree remove` + `git branch -D` from the source repo; branch deletion
  is best-effort (a merged-then-deleted branch is not an error).
- No `relay merge` verb. Integrating `relay/<name>` branches is the
  coordinator's or human's job via normal git.

### Skills (the protocol layer - sessions drive relay, not the reverse)

- `relay-coordinator` gains a "parallel workers in one repo" section:
  - when 2+ workers touch the same repo, isolate them - either
    `relay spawn --worktree`, or create worktrees yourself and pass `--dir`;
  - the merge flow: collect branch names from done messages, merge/rebase
    yourself, or escalate to the human for conflicts.
- `relay-worker` done-routine gains: commit your work on your branch before
  reporting done; the done message names the branch (see message kinds below).

### Schema

Migration 4 (existing numbered-migration mechanism in `iterm/db.py`):

```
ALTER TABLE sessions ADD COLUMN worktree_repo TEXT NOT NULL DEFAULT ''
ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'info'
```

## 2. Typed messages + broadcast

### CLI surface

```
relay send bff "spec ready at specs/be.md"                  # kind defaults to info
relay send coord "done: relay/bff has the endpoint" --kind done
relay send coord "cannot proceed, need prod creds" --kind escalation
relay send --all --project webshop "freeze: rebasing main" --kind info
```

- `--kind` accepts the known set `info | done | blocked | escalation | wake`
  plus any custom word (validated as a single lowercase token). Known kinds get
  TUI colors; custom kinds render plain. `wake` is reserved for relay's
  auto-generated messages (task assignment and blocker-cleared wake-ups are
  written with kind `wake`); the CLI refuses `--kind wake` from users.
- `--all` replaces the recipient name and requires `--project`. It queues one
  ordinary message row per registered, non-closed session in that project
  except the sender. Delivery, retention, and audit are the existing per-row
  machinery, untouched.

### Delivery and TUI

- Delivery prefix carries non-info kinds: `[relay done from bff] ...`;
  plain `info` keeps today's `[relay msg from bff] ...` (no churn for the
  common case).
- Swarm-view feed colors: done green, blocked yellow, escalation red, wake dim,
  info default, custom plain.
- **Escalation notify:** when the panel's tick first sees a queued
  `escalation` message, it fires the dangerous-prompt notify path (sound +
  macOS notification, per-session cooldown respected) immediately - even if
  the target session is busy. Delivery itself still waits for the target's
  idle prompt. Fires once per message, tracked in the panel's memory (no
  schema flag; a re-ping after a panel restart is acceptable).
- `relay msgs` prints the kind per line.

### Skills

- Workers: report completion with `--kind done` (include branch name when on a
  worktree), blockers with `--kind blocked`, and use `--kind escalation` only
  when human attention is genuinely required (it makes noise).
- Coordinator: treat `blocked`/`escalation` as interrupts, `done` as queue
  advance.

## Testing

Same no-pytest suites as the neighboring features:

- `test_cli.py`: `--worktree` validation (flag without `--dir`, non-repo dir),
  kind validation (default, custom token, `wake` refused), broadcast row
  fan-out (excludes sender and closed sessions, requires `--project`).
- `test_db.py`: migration 4 (fresh DB and upgrade path).
- `test_swarm.py`: delivery-prefix rendering per kind, feed color mapping,
  escalation notify fires once per message.
- `test_cli.py` wipe: plan includes clean worktree removal, dirty worktree kept
  with warning (temp git repos in the test, as the existing wipe tests do).
- Live paths (actual `git worktree add` + spawn into it) stay hand-checked, as
  the README already documents for the swarm layer; the git commands
  themselves are unit-tested against temp repos.

## Out of scope (explicit)

- TUI visualization upgrades (worker counts, interaction graph) - next batch,
  to be brainstormed after these ship.
- `relay merge` or any automated branch integration.
- Message threading, payloads beyond a single line, or artifact references
  (skill-level convention only: pass file paths, not content).
