---
name: relay-parked-work
description: Use when asked to pick up parked work from relay ("pick the next one from relay", "what's parked", "anything queued for me", "take the next item"), or when you have noticed a follow-up you should shelve rather than do now
---

# Relay Parked Work

Parked work is what the operator captured with `i` in the relay panel while
you were busy, instead of typing it at you mid-turn.

Run `relay help parked` before claiming anything - that is the canonical
rules text, kept there rather than here because the CLI is always present
and this skill is an install the operator can decline. Exact syntax:
`../relay-cli-reference.md`.

## Before you park a follow-up of your own

| Situation | Use instead |
| --- | --- |
| It's part of the task you're already doing | Just do it now |
| Someone specific should own it | `relay task add --owner <name>`, not --park |
| It needs a decision before it can be scoped | Ask now, or `relay send --human` |
| A genuine "not now" you'd otherwise silently drop | `relay task add "<line>" --park` |
