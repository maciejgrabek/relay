# Relay swarm CLI reference

Shared by the relay-worker and relay-coordinator skills. All verbs resolve
"me" from $ITERM_SESSION_ID automatically - run them via the Bash tool from
inside your session. Errors print to stderr with a non-zero exit.

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
        escalation | a custom lowercase token. 'escalation' also plays a
        sound + notification for the human IMMEDIATELY - use it only when a
        human decision is genuinely required. 'wake' is reserved.

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

    relay timer add --key <slug> --every <1-90> --times <1-50> --say "<text>"
        Register a timer on YOUR OWN tab: every <every> minutes, when you are
        idle at a ready prompt, <say> is typed into you and submitted. Unlike
        the other verbs this needs no `relay register` - timers bind to the tab,
        not to a swarm name, so it works in a plain unregistered Claude session.
        --key is a stable slug: re-running add with the same key UPDATES that
        timer rather than adding a second one. --times is a mandatory fire cap
        (1-50, no unlimited) - when it runs out the timer stops and a human can
        re-register it. Mode is always idle; there is no --mode flag, because
        firing mid-turn would corrupt your own turn. Payload is single line
        (newlines are flattened), so put real instructions in a file and make
        the payload a pointer to it - see the relay-self-scheduling skill.

    relay timer list
        Your own timers only: id, key, interval, on/off, fires left, next-fire
        countdown, payload. You cannot see another session's timers.

    relay timer rm --key <slug> | --id <n>
        Remove one of your own timers. --id only works for a timer on your tab.

    relay spawn --name <name> "<prompt>" [--project <p>] [--dir <path>]
                [--role worker|coordinator] [--arm off|safe|wild|insane]
                [--worktree]
        Open a new iTerm2 tab running claude, pre-registered under <name>.
        --worktree (with --dir <repo>): create branch relay/<name> and a
        sibling git worktree <repo>-<name>, and spawn the worker THERE - use it
        whenever 2+ workers touch the same repo, so they cannot clobber each
        other's files.

    relay doctor
        Print swarm health from outside the TUI: registered sessions and their
        modes, queued messages, task counts, and any orphaned work (closed
        sessions still owning tasks). Read-only; safe to run anytime.

Recovering abandoned work (a session whose tab closed while it owned tasks is
"closed"; relay detects this). These are dispositions - pick one per dead
session; run restore FIRST if you might want either, since clean/wipe destroy
the context restore needs:

    relay restore [names...] [--project <p>] [--dry-run] [--yes]
        Respawn dead workers IN THEIR ORIGINAL WORKDIR under their own name to
        CONTINUE their tasks. No names = all closed sessions owning work; naming
        a session also revives a stalled-but-open one.

    relay clean [--project <p>] [--dry-run] [--yes]
        RESET dead sessions' non-done tasks to unowned todo and remove the ghost
        rows. Someone else can then pick the tasks up.

    relay wipe [names...] [--project <p>] [--dry-run] [--yes]
    relay wipe --project <p> --all [--dry-run] [--yes]
        DELETE dead sessions' tasks + rows (orphaned scope), or with --all wipe
        an ENTIRE project (all tasks/sessions/messages). Permanent - start over.
        A relay-created worktree (spawn --worktree) is removed too, but ONLY
        when fully committed - a dirty worktree is always kept. --all never
        touches worktrees; commit your branch before reporting done so wipe
        can actually clean up.

    relay version | relay update
        Show the installed relay version / fetch + fast-forward to the latest.
