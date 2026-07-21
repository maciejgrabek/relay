"""Relay swarm CLI - the verbs Claude sessions shell out to.

    relay register --name X --role worker|coordinator [--project P]
    relay status "text"
    relay send <name> "body"
    relay inbox
    relay msgs [--with N] [--project P]
    relay task add|update|list ...        (task verbs)
    relay spawn --name X [--project P] [--dir D] "prompt"

Every verb resolves "me" from $ITERM_SESSION_ID (set by iTerm2 in every
session). Writes go straight to the SQLite bus (db.py); the relay TUI's
watcher performs deliveries. Exit codes: 0 ok, 1 user/state error (printed to
stderr so the calling Claude session sees why), 2 argparse usage error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db      # noqa: E402
import swarm   # noqa: E402


def my_iterm_id():
    """Bare session UUID. $ITERM_SESSION_ID looks like 'w0t2p0:UUID'; the
    iTerm2 Python API (and therefore the sessions table) uses just the UUID."""
    sid = os.environ.get("ITERM_SESSION_ID", "")
    if not sid:
        return None
    return sid.split(":", 1)[-1] or None


def whoami(conn):
    sid = my_iterm_id()
    return db.get_by_iterm_id(conn, sid) if sid else None


def _err(msg: str) -> int:
    print(f"relay: {msg}", file=sys.stderr)
    return 1


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _require_me(conn):
    me = whoami(conn)
    if me is None:
        return None, _err("this session is not registered - run: "
                          "relay register --name <name> --role worker|coordinator")
    return me, 0


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


# Custom message kinds are allowed but kept machine-friendly: one short
# lowercase token. Known kinds (db.MESSAGE_KINDS) get dedicated rendering.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")


# --- verb handlers (each returns an exit code) --------------------------------

def cmd_register(args) -> int:
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    name = args.name.strip()
    if not name:
        return _err("name cannot be empty")
    if name == "relay":
        return _err("'relay' is reserved - it is the sender name for system "
                    "wake-ups; pick another name")
    conn = db.connect()
    db.register(conn, name, sid, args.role, args.project or "")
    if args.dir:
        db.set_session_context(conn, name, os.path.abspath(args.dir),
                               db.get_session(conn, name)["spawn_prompt"])
    print(f"registered '{name}' as {args.role}"
          + (f" on project '{args.project}'" if args.project else ""))
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    db.set_status(conn, me["name"], args.text)
    print(f"status set: {args.text}")
    return 0


def cmd_send(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    kind = args.kind or "info"
    if kind == "wake":
        return _err("kind 'wake' is reserved for relay's automatic wake-ups")
    if not _KIND_RE.match(kind):
        return _err(f"--kind must be one short lowercase token "
                    f"(a-z, 0-9, -, _), got {kind!r}")
    if args.all:
        if args.body is not None:
            return _err("with --all, pass only the message body")
        if args.to is None:
            return _err("message body required")
        if not args.project:
            return _err("--all requires --project")
        body = args.to
        targets = [s for s in db.list_sessions(conn, args.project)
                   if s["name"] != me["name"] and not s["closed_at"]]
        if not targets:
            return _err(f"no live sessions in project '{args.project}'")
        for s in targets:
            db.queue_message(conn, me["name"], s["name"], body,
                             args.project, kind=kind)
        print(f"queued for {len(targets)} session(s): "
              + ", ".join(s["name"] for s in targets))
        return 0
    if args.to is None or args.body is None:
        return _err('usage: relay send <name> "<body>"  or  '
                    'relay send --all --project <p> "<body>"')
    if db.get_session(conn, args.to) is None:
        return _err(f"unknown recipient '{args.to}' - relay msgs shows known "
                    f"names; sessions register themselves first")
    db.queue_message(conn, me["name"], args.to, args.body, me["project"],
                     kind=kind)
    print(f"queued for {args.to} (delivered when their session is idle "
          f"and the relay TUI is running)")
    return 0


def cmd_inbox(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    msgs = db.undelivered(conn, me["name"])
    if not msgs:
        print("no new messages")
        return 0
    for m in msgs:
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        print(f"#{m['id']} from {m['from_name']}{tag} "
              f"({_ago(m['created_at'])}): {m['body']}")
        db.mark_delivered(conn, m["id"])
    return 0


def cmd_msgs(args) -> int:
    conn = db.connect()
    rows = db.message_history(conn, with_name=args.with_name,
                              project=args.project)
    if not rows:
        print("no messages")
        return 0
    for m in rows:
        tick = "" if m["delivered_at"] else "  [queued]"
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))} "
              f"{m['from_name']} -> {m['to_name']}{tag}: {m['body']}{tick}")
    return 0


def cmd_task_add(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    try:
        blockers = [int(x) for x in args.blocked_by.split(",") if x.strip()] \
            if args.blocked_by else []
    except ValueError:
        return _err(f"--blocked-by must be comma-separated task ids, got "
                    f"{args.blocked_by!r}")
    # Validate referenced ids exist: a typo'd blocker never completes, so the
    # dependent would wait forever with no signal - fail loudly at creation.
    if args.parent is not None and db.get_task(conn, args.parent) is None:
        return _err(f"--parent #{args.parent} does not exist")
    missing = [b for b in blockers if db.get_task(conn, b) is None]
    if missing:
        return _err("--blocked-by references nonexistent task(s): "
                    + ", ".join(f"#{b}" for b in missing))
    project = args.project or me["project"]
    tid = db.add_task(conn, args.title, project=project, parent_id=args.parent,
                      owner=args.owner, spec_path=args.spec,
                      blocked_by=blockers, created_by=me["name"])
    print(f"created task #{tid} [{'epic' if args.parent is None else 'subtask'}]"
          f" {args.title}")
    # Assignment wake-up - but not when assigning to yourself (a worker
    # breaking its own epic into subtasks must not spam its own inbox).
    if args.owner and args.owner != me["name"]:
        task = db.get_task(conn, tid)
        db.queue_message(conn, "relay", args.owner,
                         swarm.wakeup_assignment_body(task), project, kind="wake")
    return 0


def cmd_task_update(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    if not db.set_task_state(conn, args.id, args.state):
        return _err(f"no task #{args.id}")
    print(f"task #{args.id} -> {args.state}")
    if args.state == "done":
        # Unblock trigger: poke the owner of every task this completion fully
        # unblocked (all of its blockers are now done).
        for t in swarm.unblocked_by_completion(db.list_tasks(conn), args.id):
            if t["owner"]:
                db.queue_message(conn, "relay", t["owner"],
                                 swarm.wakeup_unblocked_body(t), t["project"],
                                 kind="wake")
    return 0


def cmd_task_list(args) -> int:
    conn = db.connect()
    owner = None
    if args.mine:
        me, rc = _require_me(conn)
        if me is None:
            return rc
        owner = me["name"]
    rows = db.list_tasks(conn, project=args.project, owner=owner)
    if not rows:
        print("no tasks")
        return 0
    # Epics first with their subtasks nested under them.
    by_parent = {}
    for t in rows:
        by_parent.setdefault(t["parent_id"], []).append(t)

    # For --mine filtering, only show blockers that are also owned by this owner
    blockers_to_show = None
    if owner is not None:
        blockers_to_show = {t["id"] for t in rows}

    def fmt(t):
        bits = [f"#{t['id']} [{t['state']}] {t['title']}"]
        if t["owner"]:
            bits.append(f"@{t['owner']}")
        bb = swarm.parse_blockers(t["blocked_by"])
        if bb:
            # When filtering by owner, only show blockers in the filtered set
            if blockers_to_show is not None:
                bb = [b for b in bb if b in blockers_to_show]
            if bb:
                bits.append("blocked-by " + ",".join(f"#{b}" for b in bb))
        if t["spec_path"]:
            bits.append(f"spec:{t['spec_path']}")
        return "  ".join(bits)

    listed = set()
    for t in by_parent.get(None, []):
        print(fmt(t))
        listed.add(t["id"])
        for c in by_parent.get(t["id"], []):
            print("    " + fmt(c))
            listed.add(c["id"])
    for t in rows:                      # orphans (parent outside the filter)
        if t["id"] not in listed:
            print(fmt(t))
    return 0


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*a, timeout=8):
    """Run a git command in the relay repo; return (rc, stdout) or (None, '')
    if git/repo is unavailable. Never raises."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", _repo_root(), *a],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return None, ""


def local_version() -> str:
    rc, out = _git("log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d")
    return out if rc == 0 and out else "unknown (not a git checkout)"


def cmd_version(args) -> int:
    print(f"relay {local_version()}")
    return 0


def _update_stamp_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_UPDATE_STAMP", "~/.relay/update-check"))


def cmd_update(args) -> int:
    """Fetch and fast-forward the relay checkout to the latest version. Safe:
    ff-only never rewrites local history, and a dirty tree or missing remote
    stops with a clear message instead of clobbering anything.

    --auto is the quiet start-up flavor bin/relay runs before the TUI boots:
    throttled to one check per day (stamp file), short fetch timeout, and
    SILENT on every skip (offline, dirty, diverged, no remote, up to date) -
    a version check must never delay or noise up a launch. It only speaks
    when it actually updated. RELAY_NO_AUTOUPDATE=1 disables it."""
    auto = getattr(args, "auto", False)
    if auto:
        if os.environ.get("RELAY_NO_AUTOUPDATE"):
            return 0
        stamp = _update_stamp_path()
        try:
            if time.time() - os.path.getmtime(stamp) < 86400:
                return 0
        except OSError:
            pass
        # Stamp the ATTEMPT, not the success - an offline day must not retry
        # the network hit on every single launch.
        try:
            d = os.path.dirname(stamp)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(stamp, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
    rc, _ = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return 0 if auto else _err(
            "not a git checkout - update by re-pulling however you "
            "installed relay")
    rc, dirty = _git("status", "--porcelain")
    if dirty:
        return 0 if auto else _err(
            "working tree has local changes - commit or stash them "
            "first, then rerun 'relay update'")
    rc, remote = _git("remote")
    if rc != 0 or not remote:
        return 0 if auto else _err("no git remote configured - nothing to "
                                   "update from")
    if not auto:
        print(f"current: {local_version()}")
        print("fetching...")
    rc, _ = _git("fetch", "--quiet", timeout=10 if auto else 30)
    if rc != 0:
        return 0 if auto else _err("git fetch failed (offline?) - try again "
                                   "when connected")
    rc, counts = _git("rev-list", "--count", "--left-right", "HEAD...@{u}")
    behind = counts.split("\t")[-1] if counts and "\t" in counts else "0"
    if behind == "0":
        if not auto:
            print("already up to date.")
        return 0
    if not auto:
        print(f"{behind} new commit(s) available, fast-forwarding...")
    rc, out = _git("merge", "--ff-only", "@{u}", timeout=30)
    if rc != 0:
        return 0 if auto else _err(
            "fast-forward failed (branch diverged) - resolve manually "
            "with git in the relay repo")
    if auto:
        print(f"relay updated: {behind} new commit(s) -> {local_version()}")
    else:
        print(f"updated: {local_version()}")
        print("restart relay (q, then run it again) to load the new version.")
    return 0


def cmd_doctor(args) -> int:
    """Print swarm health from OUTSIDE the TUI - a lifeline for 'I launched it
    and I'm stuck'. Reads the DB only; never mutates. Flags the two things that
    silently trap a user: undelivered messages piling up (relay TUI not running,
    or the target never idle) and tasks stuck in 'doing' with no movement."""
    import config as relay_config
    conn = db.connect()
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    cfg = relay_config.load()[0]
    print(f"relay doctor")
    print(f"  version: {local_version()}")
    print(f"  DB: {db.default_path()} (schema v{v})")
    print(f"  config: title_style={cfg.title_style} spawn_arm={cfg.spawn_arm} "
          f"stale_minutes={cfg.stale_minutes:g}")

    sessions = db.list_sessions(conn)
    if not sessions:
        print("  sessions: none registered")
        print("    -> nothing is in a swarm yet. Register a session, or spawn "
              "one:\n       relay spawn --name w1 --arm wild \"your task\"")
    else:
        print(f"  sessions: {len(sessions)} registered")
        for s in sessions:
            cur = db.current_task_for(conn, s["name"])
            task = f"  {cur['state']} #{cur['id']}" if cur else ""
            mode = s["mode"] or "off"
            arm = f"  arm_request={s['arm_request']}" if s["arm_request"] else ""
            print(f"    {s['name']:<14} {s['role']:<12} "
                  f"{(s['project'] or '-'):<12} mode={mode}{task}{arm}")

    queued = db.undelivered(conn)
    now = time.time()
    if queued:
        oldest_min = int((now - min(m["created_at"] for m in queued)) / 60)
        print(f"  messages: {len(queued)} queued (undelivered)")
        if oldest_min >= 2:
            print(f"    !! oldest has waited {oldest_min}m - is the relay TUI "
                  f"running? It delivers messages; if it's closed they just "
                  f"sit here.")
    else:
        print("  messages: none queued")

    tasks = db.list_tasks(conn)
    if tasks:
        from collections import Counter
        by = Counter(t["state"] for t in tasks)
        print("  tasks: " + ", ".join(f"{by[s]} {s}"
              for s in ("todo", "doing", "blocked", "done") if by[s]))
        stale_cut = cfg.stale_minutes * 60
        for t in tasks:
            if t["state"] == "doing" and now - t["updated_at"] > stale_cut:
                mins = int((now - t["updated_at"]) / 60)
                print(f"    !! possible stall: #{t['id']} '{t['title'][:40]}' "
                      f"doing, owner {t['owner'] or '?'}, no update in {mins}m")
    else:
        print("  tasks: none")

    closed = db.closed_sessions(conn)
    owners = {t["owner"] for t in tasks if t["state"] != "done" and t["owner"]}
    orphans = [s for s in closed if s["name"] in owners]
    if orphans:
        print(f"  orphans: {len(orphans)} closed session(s) still own work "
              f"- 'relay restore' to revive, 'relay clean' to reset")
        for s in orphans:
            print(f"    {s['name']} (workdir: {s['workdir'] or 'unknown'})")
    return 0


def _run_git(cwd: str, *a, timeout=8):
    """Run git in an ARBITRARY repo (unlike _git, which is pinned to relay's
    own checkout): returns (rc, stdout, stderr); (None, '', msg) on hang or
    missing git. Never raises - same hardening contract as _git."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", cwd, *a],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return None, "", str(e)


def _worktree_add(repo: str, name: str):
    """Create branch relay/<name> and a sibling worktree <repo>-<name> from
    the repo's current HEAD. Returns (worktree_path, None) on success or
    (None, error). The worktree lives NEXT TO the repo, never under ~/.relay -
    relay is a tech the session uses, not a place that owns the work."""
    rc, _, _ = _run_git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return None, f"not a git repository: {repo}"
    path = os.path.join(os.path.dirname(repo),
                        f"{os.path.basename(repo)}-{name}")
    if os.path.exists(path):
        return None, (f"worktree path already exists: {path} - pick another "
                      f"--name, or remove it (git -C {repo} worktree remove)")
    rc, _, err = _run_git(repo, "worktree", "add", path,
                          "-b", f"relay/{name}", timeout=30)
    if rc != 0:
        return None, (err or "git worktree add failed (git hung or missing?)")
    return path, None


def _worktree_dirty(workdir: str) -> bool:
    """True when the worktree has uncommitted/untracked changes - or can't be
    read at all (unreadable, hung, or missing git counts as dirty: never
    delete blind)."""
    rc, out, _ = _run_git(workdir, "status", "--porcelain")
    if rc != 0:
        return True
    return bool(out)


def _worktree_remove(repo: str, workdir: str, name: str):
    """Remove a relay-created worktree + its relay/<name> branch. Branch
    deletion is best-effort (already merged-and-deleted is not an error)."""
    rc, _, err = _run_git(repo, "worktree", "remove", workdir, timeout=30)
    if rc != 0:
        return False, (err or "git worktree remove failed")
    _run_git(repo, "branch", "-D", f"relay/{name}")
    return True, ""


def cmd_spawn(args) -> int:
    import asyncio
    import config as relay_config
    import spawn as spawnmod
    if args.worktree and not args.dir:
        return _err("--worktree requires --dir <repo>")
    workdir = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(workdir):
        return _err(f"workdir not found: {workdir}")
    repo = None
    if args.worktree:
        # The name becomes a branch (relay/<name>) and a sibling directory
        # (<repo>-<name>) - keep it a simple token so it can't redirect either.
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", args.name):
            return _err("--worktree requires a simple --name (letters, "
                        "digits, - or _): it becomes a branch and dir name")
        repo = workdir
        workdir, wt_err = _worktree_add(repo, args.name)
        if wt_err:
            return _err(wt_err)
        print(f"worktree {workdir} (branch relay/{args.name})")
    # --arm beats config [swarm] spawn_arm beats "off".
    arm = args.arm if args.arm is not None else relay_config.load()[0].spawn_arm
    try:
        sid = asyncio.run(spawnmod.spawn_worker(
            args.name, args.project or "", args.prompt, workdir, args.role,
            arm=arm))
    except Exception as e:
        if repo:
            # Undo the worktree we just created: with no session row, no
            # relay verb could ever find or clean it (untracked git state).
            ok_rm, _ = _worktree_remove(repo, workdir, args.name)
            if ok_rm:
                print(f"cleaned up worktree {workdir} after failed spawn")
        return _err(f"spawn failed: {e}")
    if repo:
        db.set_worktree_repo(db.connect(), args.name, repo)
    armed = f", arm={arm}" if arm != "off" else ""
    print(f"spawned '{args.name}' ({args.role}{armed}) in {workdir} "
          f"[session {sid[:8]}]")
    return 0


def cmd_clean(args) -> int:
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    cands = swarm.clean_candidates(sessions, tasks)
    print(swarm.clean_plan_text(cands))
    if not cands or args.dry_run:
        return 0
    if not args.yes and not _confirm(f"clean {len(cands)} session(s)?"):
        print("aborted.")
        return 0
    for c in cands:
        db.reset_owner_tasks(conn, c["name"])
        db.delete_undelivered_to(conn, c["name"])
        db.delete_session(conn, c["name"])
    print(f"cleaned {len(cands)} session(s).")
    return 0


def cmd_wipe(args) -> int:
    import swarm
    conn = db.connect()
    if args.all and args.names:
        return _err("--all takes no session names (it wipes the whole project)")
    if args.all:
        if not args.project:
            return _err("--all requires --project (refusing to wipe every "
                        "project at once)")
        nt = len(db.list_tasks(conn, project=args.project))
        ns = len(db.list_sessions(conn, project=args.project))
        nm = len(db.message_history(conn, project=args.project, limit=10**9))
        print(swarm.wipe_plan_text([], project_all=(nt, ns, nm)))
        if args.dry_run:
            return 0
        if not (nt or ns or nm):
            return 0
        if not args.yes and not _confirm(
                f"permanently DELETE all of project '{args.project}' "
                f"({nt} tasks + {ns} sessions + {nm} messages)?"):
            print("aborted.")
            return 0
        nt2, ns2, nm2 = db.wipe_project(conn, args.project)
        print(f"wiped project '{args.project}': {nt2} tasks, {ns2} sessions, "
              f"{nm2} messages.")
        return 0

    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.wipe_candidates(sessions, tasks, names=names)
    for c in cands:
        if (c.get("worktree_repo") and c.get("workdir")
                and os.path.isdir(c["workdir"])):
            c["worktree_action"] = ("keep-dirty"
                                    if _worktree_dirty(c["workdir"])
                                    else "remove")
    print(swarm.wipe_plan_text(cands))
    for w in swarm.wipe_blocker_warnings(cands, tasks):
        print("  " + w)
    if not cands or args.dry_run:
        return 0
    total_tasks = sum(len(c["task_ids"]) for c in cands)
    if not args.yes and not _confirm(
            f"permanently DELETE {total_tasks} task(s) + {len(cands)} "
            f"session(s)?"):
        print("aborted.")
        return 0
    still_closed = {r["name"] for r in db.closed_sessions(conn, args.project)}
    acted = 0
    for c in cands:
        if c["name"] not in still_closed:
            print(f"  skipped {c['name']} - revived since the plan")
            continue
        db.delete_tasks_by_ids(conn, c["task_ids"])
        db.delete_undelivered_to(conn, c["name"])
        db.delete_session(conn, c["name"])
        if c.get("worktree_action") == "remove":
            ok_rm, rm_err = _worktree_remove(c["worktree_repo"], c["workdir"],
                                             c["name"])
            print(f"  removed worktree {c['workdir']}" if ok_rm
                  else f"  worktree removal failed: {rm_err}")
        acted += 1
    print(f"wiped {acted} session(s).")
    return 0


def cmd_restore(args) -> int:
    import config as relay_config
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.list_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.restore_candidates(sessions, tasks, names=names)
    spawn_arm = relay_config.load()[0].spawn_arm
    missing = {c["name"] for c in cands
               if c["workdir"] and not os.path.isdir(c["workdir"])}
    print(swarm.restore_plan_text(cands, spawn_arm, missing_workdirs=missing))
    # only candidates we can actually revive (workdir set AND still exists)
    doable = [c for c in cands if c["workdir"] and os.path.isdir(c["workdir"])]
    if not doable or args.dry_run:
        return 0
    if not args.yes and not _confirm(f"restore {len(doable)} session(s)?"):
        print("aborted.")
        return 0
    import asyncio
    import spawn as spawnmod
    for c in doable:
        # A restore is a fresh spawn: its arm level must follow spawn_arm, not
        # the dead worker's stale persisted mode. Clear it first so an off
        # spawn_arm comes back off (matching the plan's warning); wild/insane
        # re-arm + re-persist via spawn_worker's arm_request.
        db.set_session_mode(conn, c["name"], "")
        prompt = swarm.resume_prompt(c["name"], c["project"], c["role"],
                                     c["spawn_prompt"])
        asyncio.run(spawnmod.spawn_worker(
            c["name"], c["project"], prompt, c["workdir"], c["role"],
            arm=spawn_arm))
        print(f"restored {c['name']} in {c['workdir']}")
    return 0


# --- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)

    r = sub.add_parser("register", help="bind this session to a swarm name")
    r.add_argument("--name", required=True)
    r.add_argument("--role", required=True, choices=db.ROLES)
    r.add_argument("--project", default="")
    r.add_argument("--dir", default=None,
                   help="record this session's working directory (for restore)")
    r.set_defaults(fn=cmd_register)

    s = sub.add_parser("status", help="update my one-line status")
    s.add_argument("text")
    s.set_defaults(fn=cmd_status)

    sd = sub.add_parser("send", help="queue a message to a named session")
    sd.add_argument("to", nargs="?", default=None,
                    help="recipient name (omit with --all)")
    sd.add_argument("body", nargs="?", default=None)
    sd.add_argument("--kind", default="info",
                    help="info|done|blocked|escalation or a custom lowercase "
                         "token ('wake' is reserved)")
    sd.add_argument("--all", action="store_true",
                    help="broadcast to every live session in --project "
                         "(except me)")
    sd.add_argument("--project", default=None)
    sd.set_defaults(fn=cmd_send)

    ib = sub.add_parser("inbox", help="print + mark delivered my queued messages")
    ib.set_defaults(fn=cmd_inbox)

    ms = sub.add_parser("msgs", help="message history")
    ms.add_argument("--with", dest="with_name", default=None)
    ms.add_argument("--project", default=None)
    ms.set_defaults(fn=cmd_msgs)

    t = sub.add_parser("task", help="task board verbs")
    tsub = t.add_subparsers(dest="task_verb", required=True)

    ta = tsub.add_parser("add", help="create a task (no --parent = epic)")
    ta.add_argument("title")
    ta.add_argument("--parent", type=int, default=None)
    ta.add_argument("--owner", default=None)
    ta.add_argument("--spec", default=None)
    ta.add_argument("--blocked-by", dest="blocked_by", default=None)
    ta.add_argument("--project", default=None)
    ta.set_defaults(fn=cmd_task_add)

    tu = tsub.add_parser("update", help="change a task's state")
    tu.add_argument("id", type=int)
    tu.add_argument("--state", required=True, choices=db.TASK_STATES)
    tu.set_defaults(fn=cmd_task_update)

    tl = tsub.add_parser("list", help="list tasks (epics with nested subtasks)")
    tl.add_argument("--project", default=None)
    tl.add_argument("--mine", action="store_true")
    tl.set_defaults(fn=cmd_task_list)

    sp = sub.add_parser("spawn", help="open an iTerm2 tab running claude, "
                                      "pre-registered under --name")
    sp.add_argument("prompt")
    sp.add_argument("--name", required=True)
    sp.add_argument("--project", default=None)
    sp.add_argument("--dir", default=None)
    sp.add_argument("--role", default="worker", choices=db.ROLES)
    sp.add_argument("--arm", default=None,
                    choices=("off",) + db.ARM_REQUEST_MODES,
                    help="arm level the watcher applies to the new worker "
                         "(default: config [swarm] spawn_arm)")
    sp.add_argument("--worktree", action="store_true",
                    help="create a git worktree of --dir (branch relay/<name>, "
                         "sibling dir <repo>-<name>) and spawn the worker there")
    sp.set_defaults(fn=cmd_spawn)

    dr = sub.add_parser("doctor", help="print swarm health from outside the TUI")
    dr.set_defaults(fn=cmd_doctor)

    vr = sub.add_parser("version", help="print the installed relay version")
    vr.set_defaults(fn=cmd_version)

    up = sub.add_parser("update", help="fetch + fast-forward to the latest relay")
    up.add_argument("--auto", action="store_true",
                    help="quiet start-up check: throttled daily, silent when "
                         "offline/dirty/current (used by bin/relay)")
    up.set_defaults(fn=cmd_update)

    cl = sub.add_parser("clean", help="reset abandoned tasks + remove dead "
                                      "sessions")
    cl.add_argument("--project", default=None)
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--dry-run", dest="dry_run", action="store_true")
    cl.set_defaults(fn=cmd_clean)

    rs = sub.add_parser("restore", help="respawn dead workers in their workdir "
                                        "to finish their tasks")
    rs.add_argument("names", nargs="*", help="specific sessions to restore "
                    "(default: all closed sessions owning work)")
    rs.add_argument("--project", default=None)
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--dry-run", dest="dry_run", action="store_true")
    rs.set_defaults(fn=cmd_restore)

    wp = sub.add_parser("wipe", help="DELETE dead sessions' tasks (or a whole "
                                     "project with --all)")
    wp.add_argument("names", nargs="*", help="specific closed sessions to wipe "
                    "(default: all closed sessions)")
    wp.add_argument("--project", default=None)
    wp.add_argument("--all", action="store_true",
                    help="with --project: delete the ENTIRE project "
                         "(all tasks/sessions/messages, even live)")
    wp.add_argument("--yes", action="store_true")
    wp.add_argument("--dry-run", dest="dry_run", action="store_true")
    wp.set_defaults(fn=cmd_wipe)

    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:          # argparse exits itself; surface its code
        return int(e.code or 0)
    try:
        return args.fn(args)
    except Exception as e:
        return _err(str(e))


if __name__ == "__main__":
    sys.exit(main())
