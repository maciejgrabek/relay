# Relay swarm CLI reference

Shared by the relay-worker and relay-coordinator skills. All verbs resolve
"me" from $ITERM_SESSION_ID automatically - run them via the Bash tool from
inside your session. Errors print to stderr with a non-zero exit.

    relay join [<name>] [--role worker|coordinator] [--project <p>]
        START HERE. Registers this session AND prints, in one go: who else is
        in the swarm, anything already queued for you, and the protocol you
        are expected to follow. The name is OPTIONAL - with none, relay
        derives one from your working directory, so "use relay to talk to the
        other sessions" needs no naming ceremony. Passing a name later renames
        you in place, carrying your messages and tasks with you. Safe to
        re-run. `relay register` is the same binding without the teaching.

    relay who
        Who else is here: names, roles, status lines, how recently seen. This
        is how you find out who you can talk to. Read-only - it does not
        register you.

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
        escalation | a custom lowercase token. 'escalation' also plays a
        sound + notification for the human IMMEDIATELY - use it only when a
        human decision is genuinely required. 'wake' is reserved.

    relay send --pr <owner/name>#<n> "<body>" [--kind <k>]
        Route a message to whichever session claimed that PR. Exit 0 and
        prints the owner it resolved to. Exit 3 = unclaimed (nobody ran
        `relay pr claim`). Exit 4 = the owner session is gone (closed, or its
        name was rebound to a different tab). Relay never guesses: it will not
        hand the PR to a different worker, because a session with no context
        on that branch produces a plausible fix that misses the point. On 3 or
        4, batch the misses and escalate once with --human.

    relay send --human "<body>"
        Escalate to the operator. Plays the sound, posts the notification, and
        shows in the swarm feed. It is NEVER injected into any session, so use
        it for the decisions only a human can make. Batch a sweep's misses into
        one message rather than firing one per PR.

    relay reply "<body>" [--kind <k>]
    relay reply <msg-id> "<body>" [--kind <k>]
        Answer whoever wrote to you, without retyping their name. With no id
        it answers the last message you received. If your last delivery was a
        BATCH of several, it refuses and lists the ids - pick one. Sets a
        correlation link, so `relay ask` on the other side recognises it.

    relay ask <name> "<question>" [--wait <seconds>]
        Ask one session and BLOCK until it answers, printing the answer as
        this command's output. The answer arrives inside your current turn -
        you do not have to stop and wait to be woken. Default wait 120s, max
        540s. On timeout it exits non-zero and the question stays queued, so
        it degrades into an ordinary message; end your turn and relay wakes
        you with the reply. Not a discussion: no rounds, no agreement.

    relay discuss <name> [<name>...] "<the question>" [--rounds N]
    relay say <id> "<your view>"
    relay agree <id> "<the position>"
    relay close <id> "<how it ended>"
    relay thread <id>
        Get several sessions to SETTLE something. `discuss` opens a thread
        (topic goes LAST) where everyone sees everyone's posts. Participants
        are woken with a pointer; `relay thread <id>` is the read path and
        shows the transcript, who has settled, and what you can do next.
        N (default 3) is a SUGGESTED post budget, not a limit - relay tells
        you when you pass it and never refuses a post for being long. `agree`
        does not spend budget; posting after agreeing retracts your agreement.
        THE ONE REFUSAL: `say`, `agree` and `close` are blocked while posts
        are waiting for you. The refusal PRINTS them and marks them read, so
        re-running your command goes through - it costs a bash call, not a
        turn. Reading the transcript with `relay thread <id>` clears them the
        same way.
        THE DECISION IS YOURS. Relay marks a discussion settled only when
        every participant has agreed, and never judges one failed or routes it
        to the human. Any other ending is yours to declare with `relay close`,
        and if it genuinely needs a human, YOU say so with
        `relay send --human`. Full rules: relay help discuss

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

    relay next
        Claim the oldest parked item for THIS directory and print it with the
        context stamp taken at capture time. Claiming sets state=doing and
        un-parks it, so from there it is an ordinary task. Needs no
        `relay register` - parked work binds to a directory, not a swarm name,
        so it works in a plain unregistered session. Mine-before-unowned, then
        oldest first; an item parked for a DIFFERENT session is never handed
        to you. Distinguishes "nothing parked here at all" from "there is
        parked work here but none of it is yours" - the second case names the
        count and points you at `relay parked` instead of pretending the
        directory is empty. Full rules: relay help parked

    relay parked [--all] [--drop <id>]
        List parked work without claiming any of it. Default is this
        directory; --all is every directory. Each item prints an owner line
        only when it has one - unowned is the normal case for a freshly
        parked item, and the owner line is what tells you an item is
        earmarked for someone else rather than up for grabs. --drop removes
        one (parked items only - it can never delete real work).

    relay task add "<title>" --park
        Shelve a follow-up YOU noticed instead of doing it now. Lands unowned
        in this directory for whoever runs `relay next` here. Park what you
        would otherwise silently drop, not everything you noticed.

    relay pr set <owner/name>#<n> --state created|review|changes|approved|merged|closed
                  [--title <t>] [--branch <b>] [--project <p>]
        Push a PR's CURRENT state into relay. Relay never calls gh and never
        looks at GitHub - it stores what you tell it, and everything that
        displays a state also displays how old that report is. Run this for
        every PR your sweep sees, claimed or not: an unclaimed PR that relay
        knows about shows up as UNCLAIMED instead of being invisible.

    relay pr claim <owner/name>#<n> [--task <id>] [--branch <b>]
        Record that THIS session opened this PR. Run it immediately after
        `gh pr create`, in the same breath as committing. This is the only
        thing that makes "which session did this PR" answerable later - a PR
        you do not claim can never be routed back to you automatically.

    relay pr list [--project <p>] [--mine] [--days <n>]
        PRs in stable order (repo, then number) with state, age of that state,
        owner, task, and an UNCLAIMED or GONE marker. --days defaults to
        RELAY_PR_RETENTION_DAYS (7).

    relay timer add --key <slug> --every <1-90> --times <1-50> --say "<text>"
        Register a timer on YOUR OWN tab: every <every> minutes, when you are
        idle at a ready prompt, <say> is typed into you and submitted. Unlike
        the other verbs this needs no `relay register` - timers bind to the tab,
        not to a swarm name, so it works in a plain unregistered Claude session.
        --key is a stable slug: re-running add with the same key UPDATES that
        timer's interval/payload in place rather than adding a second one, and
        its cap too - but ONLY while the timer still has fires left. An
        exhausted timer (fire cap reached) keeps its existing cap regardless
        of --times, and re-running add never revives it, nor a timer an
        operator turned off / left pending-restore after a relay restart;
        only the operator can re-arm it, from the `t` overlay. --times
        is a mandatory fire cap (1-50, no unlimited). --every must be a whole
        number of minutes - junk (e.g. a typo'd unit like "60m") is rejected,
        not silently clamped to 1. Limited to 5 self-registered timers per tab
        (operator-created timers on the same tab don't count), checked only
        when registering a brand-new key. Mode is always idle; there is no
        --mode flag, because firing mid-turn would corrupt your own turn.
        Payload is single line (newlines are flattened), so put real
        instructions in a file and make the payload a pointer to it - see the
        relay-self-scheduling skill.

    relay timer list
        Your own timers only: id, key, interval, on/off, fires left, next-fire
        countdown, payload. This verb has no flag to list another session's
        timers - it is scoped to your own tab, not a security boundary.

    relay timer rm --key <slug> | --id <n>
        Remove one of your own timers. --id only works for a timer on your tab.

    relay spawn --name <name> "<prompt>" [--project <p>] [--dir <path>]
                [--role worker|coordinator] [--arm off|safe|wild|insane]
                [--worktree] [--share]
        Open a new iTerm2 tab running claude, pre-registered under <name>.
        --worktree (with --dir <repo>): create branch relay/<name> and a
        sibling git worktree <repo>-<name>, and spawn the worker THERE - use it
        whenever 2+ workers touch the same repo, so they cannot clobber each
        other's files.
        Two REFUSALS you will meet, both conditions relay can check rather
        than advice it can only give:
          - no arm level (no --arm, no [swarm] spawn_arm) -> refused. An
            unarmed worker stops at its first permission prompt with nobody
            at that tab to clear it. Pass --arm wild, or --arm off if you
            will sit there yourself.
          - another live worker already in that --dir -> refused. Add
            --worktree, or --share if the new session will only read there.

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
