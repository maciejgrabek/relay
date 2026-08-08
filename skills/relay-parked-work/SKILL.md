---
name: relay-parked-work
description: Use when asked to pick up parked work from relay ("pick the next one from relay", "what's parked", "anything queued for me", "take the next item"), or when you have noticed a follow-up you should shelve rather than do now
---

# Relay Parked Work

While you were busy, the operator had a thought and parked it in relay rather
than typing it at you - which would have pulled this session toward work that
was not the point of the turn. Parked items are addressed by DIRECTORY, so
anything parked in your workdir is yours to take.

Exact syntax: `relay help parked`, or `../relay-cli-reference.md`. This skill
covers only the judgment the CLI cannot check for you.

## Relay never pushes parked work at you

The reason an item was parked is that the operator did not want it yet. Relay
can inject into an idle tab and deliberately does not do so with this, because
a backlog that drains itself is drift, not help.

## The rules

**A parked item is a seed, not a spec.** It was captured in three seconds while
the operator was mid-something-else. `relay next` prints the context stamp -
what the session was doing when the thought arrived. Read it. If the line is
still ambiguous, ask. Do not infer scope from seven words.

**Claim one, then stop.** Finishing an item and immediately claiming the next
is the self-draining backlog relay refuses to build.

**Hand back what is stale.** If the premise no longer holds, say so instead of
implementing it. A three-day-old line does not know what changed.

**Once claimed it is ordinary work.** Finish with
`relay task update <id> --state done`.

## Surface, never claim

When you finish a task, run `relay parked` and tell the operator what is there
- then stop. They decide.

    Done. 3 items parked in /Work/relay:
      retry backoff on inject
      widget shows parked count
      +1 more
    Want me to take one?

## Parking your own follow-ups

`relay task add "<line>" --park` shelves something you noticed but should not
do now. It lands unowned in this directory.

Park what you would otherwise silently drop - not everything you noticed. A
backlog nobody reads is worse than no backlog, and the operator sees the count
on every tab's status bar.
