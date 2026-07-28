---
name: relay-self-scheduling
description: Use when asked to take standing responsibility for something on an interval ("you're responsible for PRs", "check X every N minutes", "register a timer in relay") - writes a durable prompt file and registers a capped relay timer bound to this tab
---

# Relay Self-Scheduling

You can register a timer that injects text back into **your own tab** on an
interval. Flags and exact syntax: `skills/relay-cli-reference.md`, or
`relay timer add --help`. This skill covers only the judgment the CLI cannot
check for you.

## First: should this be a timer at all?

Most "keep an eye on X" requests should NOT become a relay timer.

| Situation | Use instead |
| --- | --- |
| One follow-up at a known time | Just do it now, or say when you will |
| You need to poll something inside a single turn | A loop in the turn |
| The work belongs to a different session | `relay send` - queued, not scheduled |
| The interval is "whenever I next feel like it" | Nothing. Ask. |

A relay timer earns its place when **all** of these hold:
- The responsibility is standing, not a one-off.
- It must run when nobody is watching the terminal.
- Each firing is useful even if nothing has changed since the last one.

If any of those is false, say so and do not register one.

## Write the prompt file first, then register a pointer

Never put the real instructions in `--say`. Two reasons, both fatal:

1. Payloads are single-line - embedded newlines are collapsed at save time, so
   a good multi-paragraph prompt becomes one unreadable line.
2. By the third firing your context has likely been compacted. A payload that
   assumes you remember this conversation will not work; a pointer to a
   self-contained file will.

So:

```
1. Write .relay/prompts/<key>.md - self-contained. Write it for a reader who
   has never seen this conversation: what to check, where, what counts as
   done, what to do if there is nothing to do.
2. relay timer add --key <key> --every 20 --times 10 \
     --say "Read .relay/prompts/<key>.md and do what it says."
```

Worked example, "you are responsible for PRs":

```bash
# .relay/prompts/pr-duty.md holds the actual duty description
relay timer add --key pr-duty --every 20 --times 10 \
  --say "Read .relay/prompts/pr-duty.md and do what it says."
```

## Interval and cap sanity

The CLI clamps to 1-90 minutes and 1-50 fires. Both clamps permit choices you
should not make.

- **Interval:** 15-30 minutes for review/monitoring duties. Under 10 minutes
  means you fire before the previous turn's work has landed. A 1-minute
  self-firing timer is pathological - the clamp allows it, do not use it.
- **Cap:** pick the number that covers the session you are actually in.
  `--times 10` at 20-minute intervals is a bounded ~3-hour shift. When it runs
  out, the human is back and can re-register. That is the design, not a
  limitation - there is no unlimited on this path.

Multiply before you commit: `--every 2 --times 50` is over an hour and a half
of near-continuous unattended token burn.

## Tell the human what you did

After registering, state the key, interval, cap, and total wall-clock span in
one line. They can kill it with `x` in relay's `t` overlay, and they should
know it exists before they walk away.

## Clean up

When the responsibility ends, remove it:

```bash
relay timer list                 # this session's timers only
relay timer rm --key pr-duty
```

## Two things you cannot do

- **Schedule into another session.** Timers bind to your own tab. Use
  `relay send` to reach another session.
- **Use `now` mode.** It injects mid-turn, which would corrupt your own turn.
  It is operator-only in the `t` overlay, and the flag does not exist on the
  CLI.
