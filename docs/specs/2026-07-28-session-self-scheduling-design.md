# Session Self-Scheduling (`relay timer`) - Design Spec

**Date:** 2026-07-28
**Status:** Approved for planning
**Builds on:** `docs/specs/2026-07-24-session-timers-design.md` (session timers v1)

## Summary

Let a Claude session register its own timer, from inside its own tab, via a new
`relay timer` CLI verb group. The operator stops authoring payloads they cannot
write well and instead delegates the authoring to the session that actually has
the context:

> "You are responsible for PRs. Write a prompt you will understand later and
> register it as a relay timer."

Origin: operator request. This is a new **authoring surface** for the existing
timers feature, not a new scheduling engine. `iterm/timers.py` and the watcher
fire path are untouched; rows created by the CLI are ordinary timer rows.

## 1. Why the CLI and not the overlay

The `t` overlay (timers v1 §4) assumes the human writes the payload. That is the
wrong principal for standing responsibilities:

- The operator does not know what phrasing *this* session, with *this*
  accumulated context, will act on correctly. The session does.
- The operator is not at the panel at 02:00. The point of a standing
  responsibility is that it runs while nobody is watching.

The precedent is settled: relay already exposes `relay spawn`, which lets a
session **open new iTerm2 tabs running Claude**. A session scheduling text into
its own tab is strictly less powerful than that. If `spawn` is acceptable from a
session, `timer add` is.

## 2. Behavior model (the locked decisions)

- **Live immediately.** `relay timer add` inserts an `enabled = 1, active = 1`
  row, exactly like the overlay does. Timers v1 §6 already states the rule:
  creating a timer during a live run IS the deliberate act. There is no
  operator-approval queue.
- **The restart gate still applies.** Per timers v1 §6, every saved timer loads
  `active = 0` on relay startup. A self-registered timer therefore does **not**
  silently survive a relay restart; it becomes pending-restore like any other.
  This is the safety property that makes "live immediately" acceptable.
- **Bound to the tab, not to a swarm name.** Resolution is `my_iterm_id()` only.
  `relay timer add` does **not** call `_require_me` and does **not** require
  `relay register`. Timers v1 §2 is explicit that timers work on any session
  including a plain Claude tab or a bare shell; gating this path on swarm
  registration would regress that, and the primary use case (a lone Claude tab
  told "you own PRs") is exactly the unregistered case.
- **Own tab only.** A session can schedule into itself and nothing else.
  Cross-session instruction is already covered by `relay send`, which is queued
  rather than scheduled - the correct shape for that job.

## 3. The three guards

All four live in the CLI verb. The engine, the watcher, and `timers.py` are
unchanged; these rows are indistinguishable from overlay-authored rows once
written.

| Guard | Rule | Why |
| --- | --- | --- |
| **Forced idle mode** | `mode` is always `"idle"`. `--mode` is not exposed; `--mode now` is an error naming the overlay as the operator-only route. | `now` injects mid-turn (timers v1 §9). A session scheduling an interrupt for *itself* mid-turn produces garbled input at best and a corrupted turn at worst. |
| **Mandatory fire cap** | `--times` is required, clamped to `[1, 50]`. `--times 0` (unlimited) is rejected. | `db.add_timer` already defaults `max_fires = 10`; this path makes the ceiling non-negotiable. Unbounded self-injection with no human present is a token bonfire. A session that wants more must come back and re-register. |
| **Upsert by key** | `--key <slug>` is required. `(iterm_session_id, key)` is unique; re-running `add` with the same key updates the existing row in place. | The duplicate cascade: timer fires, session does the work, session helpfully registers another timer. Now there are two, then four. The key makes re-registration idempotent. |

**No own-panel guard, deliberately.** The overlay rejects relay's own tab, but
the CLI cannot: `_own_sid` is derived from the panel process's own
`$ITERM_SESSION_ID` (`app.py:893`) and is never persisted, so a CLI running in
any other tab has no way to learn it. Persisting it just for this would be a new
surface for a case that cannot occur in practice - the panel tab runs the TUI,
not a shell where anyone could type `relay timer add`. And the engine already
covers it unconditionally: `_fire_timers` returns on
`info.session_id == self.own_sid` (`watcher.py:818`) before any timer is
considered. Engine skip is the real protection; the editor check was only ever
a courtesy.

Everything downstream is inherited unchanged: pause freezes, `require_armed`
policy, dry-run would-fire, `needs_reconfirm` bind-age deactivation, audit
before act, one fire per session per tick.

## 4. Surface

```
relay timer add --key <slug> --every <1-90> --times <1-50> --say "<text>"
relay timer list
relay timer rm <id> | --key <slug>
```

- **`add`** - resolves the tab via `my_iterm_id()`; clamps `--every` through
  `timers.clamp_interval`; runs `--say` through `timers.sanitize_payload`;
  writes via `db.add_timer` (or `db.update_timer` when the key already exists on
  this session). Prints the resulting row and when it first fires.
- **`list`** - `db.list_timers(conn, my_iterm_id())`, rendered as id, key,
  interval, fires left, next due, payload. Own session only.
- **`rm`** - delete by id or by key, restricted to this session's rows so one
  tab can never remove another tab's timers.

**Key validation:** `^[a-z][a-z0-9_-]{0,23}$`, reusing the shape of the existing
`_KIND_RE` convention in `cli.py`.

### Teaching errors

The CLI is the only guidance that reaches a session which never loaded a skill,
so every rejection carries the fix:

- Missing `--key` -> names the flag and explains the duplicate cascade in one
  line.
- Missing `--times` -> states the cap is mandatory and suggests 10.
- `--mode now` -> "operator-only; use the `t` overlay".
- `--say` longer than ~200 chars **warns** (does not fail) and points at the
  prompt-file pattern in §5.

## 5. The authoring pattern (prompt file, not inline payload)

The documented pattern, carried by the skill (§6) and `relay-cli-reference.md`:

```
1. Write durable instructions to .relay/prompts/<key>.md
   - self-contained; assume the reader has no memory of this conversation
2. relay timer add --key pr-duty --every 20 --times 10 \
     --say "Read .relay/prompts/pr-duty.md and do what it says."
```

The indirection is load-bearing, not stylistic:

- `sanitize_payload` collapses newlines to spaces, so any genuinely good
  multi-paragraph prompt becomes one enormous single line.
- By fire #3 the session has likely compacted several times and no longer
  remembers why it is "responsible for PRs". A pointer to a self-contained file
  survives that; a payload that assumes conversational context does not.
- The file is diffable, reviewable, and operator-editable without touching the
  timer row.

`.relay/prompts/` is per-project (relative to the session's workdir), created on
demand by the session. Relay itself neither creates nor reads it - the path is
just text inside a payload.

## 6. The skill

New standalone skill `skills/relay-self-scheduling/SKILL.md` (~40 lines).

```
name: relay-self-scheduling
description: Use when asked to take standing responsibility for something on an
  interval ("you're responsible for PRs", "check X every N minutes", "register a
  timer in relay") - writes a durable prompt file and registers a capped relay
  timer bound to this tab
```

**Standalone, not a `relay-worker` section.** `relay-worker`'s description gates
on *"told you are a relay swarm worker"*, so a plain Claude tab told "you own
PRs" would never load it - and that is the primary case. Both existing skills
cross-reference the new one instead of duplicating it.

**The skill carries judgment; the CLI carries mechanics.** Guardrails at the
point of use always fire; documentation only fires if the skill loaded. So the
skill must NOT restate flag syntax (that lives in `relay-cli-reference.md`).
It covers only what the CLI cannot check:

1. **When not to register one.** Many "keep an eye on X" asks are better served
   by `/loop`, a one-shot, or nothing at all. This section comes first.
2. **Writing a payload that survives amnesia.** The CLI can flag a long inline
   `--say`; it cannot tell whether `.relay/prompts/pr-duty.md` is self-contained
   or quietly assumes context that will be compacted away.
3. **Interval and cap sanity.** 20 minutes x 10 fires is a bounded 3-hour shift.
   2 minutes x 50 is a bonfire. The clamps permit both.
4. **Cleanup.** `relay timer list` / `rm` when the responsibility ends.

## 7. Data model

One migration, extending the timers table from timers v1 §2:

```python
6: ("ALTER TABLE timers ADD COLUMN key TEXT NOT NULL DEFAULT ''",)
```

plus `_CURRENT_VERSION` bumped `6 -> 7` and `key TEXT NOT NULL DEFAULT ''`
added to the `timers` block of `_SCHEMA`. Consistent with the existing numbered
`_MIGRATIONS` ladder in `db.py` (last key 5, `_CURRENT_VERSION` 6). Fresh DBs
get the column from `_SCHEMA`; "column already present" is swallowed by
`_migrate`, as with migration 5. (`key` is a non-reserved keyword in SQLite and
works unquoted in column position, including in `update_timer`'s generated
`f"{k}=?"` clause - verified.)

Uniqueness of `(iterm_session_id, key)` is enforced in the CLI verb (lookup then
insert-or-update), not by a DB constraint - overlay-authored rows keep
`key = ''` and would otherwise all collide.

Self-registered rows also set `label = "self:<key>"`. This gives the operator a
free visual marker in the `t` overlay and the preview TIMERS block: "the session
asked for this, I did not."

## 8. Visibility

No new UI. Self-registered timers appear wherever timers v1 §8 already shows
them - the `⏲N` row glyph, the preview TIMERS block, and the `TIMER ->` feed
line - distinguished only by the `self:` label prefix. The operator kills one
with `x` in the `t` overlay exactly as they would their own.

## 9. Safety and edge cases

- **Runaway self-registration:** the `--key` upsert makes re-registration
  idempotent; the mandatory cap bounds the damage even if a session invents a
  new key each time (each run is bounded, and every row is visible in the
  overlay).
- **Relay not running:** the row is written and simply waits; on relay's next
  start it goes through the normal pending-restore flow (timers v1 §6).
- **Session unregistered in the swarm:** fine by design (§2). `label` falls back
  to `self:<key>` with no swarm name.
- **Stale binding:** unchanged - `bound_at` is set on insert, and
  `needs_reconfirm` deactivates a too-old binding instead of firing
  (timers v1 §5).
- **Pathologically short interval:** `clamp_interval` permits 1 minute. We do
  **not** add a higher floor in code; the skill says a self-firing 1-minute
  timer is pathological. Code enforces bounds, prose enforces taste.
- **Payload referencing a file that never gets written:** the fire still
  happens; the session reads a missing file and says so. Cheap, visible,
  self-correcting - not worth a pre-flight check in the CLI.

## 10. Module layout

```
iterm/cli.py                        # MODIFY: `timer` verb group (add/list/rm)
iterm/db.py                         # MODIFY: migration 6 (key column) + _SCHEMA
iterm/test_cli.py                   # MODIFY: guard + upsert coverage
iterm/test_db.py                    # MODIFY: key column migration + upsert
skills/relay-self-scheduling/SKILL.md   # NEW
skills/relay-cli-reference.md       # MODIFY: `relay timer` verbs
skills/relay-worker/SKILL.md        # MODIFY: cross-reference
skills/relay-coordinator/SKILL.md   # MODIFY: cross-reference
README.md                           # MODIFY: self-scheduling section
```

`iterm/timers.py`, `iterm/watcher.py`, and `iterm/app.py` are **unchanged**.
That is the point of the design: this is an authoring surface, not an engine.

## 11. Testing

- `test_cli.py` - interval clamping at both bounds; forced idle mode
  (`--mode now` rejected); `--times` required and clamped to `[1, 50]`,
  `--times 0` rejected; `--key` required and format-validated; upsert by key
  updates rather than inserts; `rm` cannot touch another session's row; `list`
  shows only own rows; long `--say` warns but succeeds; no `$ITERM_SESSION_ID`
  is a clean error.
- `test_db.py` - migration 6 applies to a pre-migration DB and is idempotent;
  `key` round-trips; fresh-schema DBs have the column.
- No `test_timers.py` or `test_watcher.py` changes - the engine is untouched,
  which is itself the assertion.

## 12. Out of scope

- Cross-session scheduling (use `relay send`).
- `now` mode from the CLI (operator-only, permanently).
- Unlimited self-registered timers.
- Relay creating or validating `.relay/prompts/` contents.
- An operator-approval queue for self-registered timers (rejected: it kills the
  unattended use case, and the restart gate already provides the human
  checkpoint).
- Multi-line payloads - still deferred from timers v1 §12; the prompt-file
  pattern is the answer instead.
