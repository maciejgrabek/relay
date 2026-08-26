"""Tests for ~/.relay/workspaces.toml. Temp files, no iTerm2 or sqlite imports.

Run: python3 iterm/test_wsconfig.py    or    ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import wsconfig  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def _err(text):
    """Return the ConfigError message raised by loading `text`, or ''."""
    try:
        wsconfig.load(_write(text))
        return ""
    except wsconfig.ConfigError as exc:
        return str(exc)


FULL = '''
[settings]
target = "current"
warmup = 2.0

[[dragen]]
name = "monitor"
dir = "~/Work/dragen"
cmd = "top"

[[dragen]]
name = "DRAGEN DOCS"
dir = "~/Work/dragen"
cmd = "claude"
arm = "safe"

[[dragen]]
name = "ingest"
dir = "~/Work/dragen"
cmd = "claude"
arm = "wild"
prompt = "audit the ingest pipeline"
role = "worker"
project = "dragen"

[[dragen]]
name = "logs"
dir = "~/Work/dragen"
cmd = "tail -f log/dev.log"
window = 2
panes = [ { cmd = "ps aux", split = "h" } ]
'''


def run():
    ok = True

    settings, spaces = wsconfig.load(_write(FULL))
    ok &= check("settings target is read", settings.get("target") == "current")
    ok &= check("settings warmup is read", settings.get("warmup") == 2.0)
    ok &= check("one workspace is found", list(spaces) == ["dragen"])
    tabs = spaces["dragen"]
    ok &= check("four tabs are found", len(tabs) == 4)
    ok &= check("dir is expanded",
                tabs[0].dir == os.path.expanduser("~/Work/dragen"))
    ok &= check("a plain tab has no arm", tabs[0].arm == "")
    ok &= check("arm is read", tabs[1].arm == "safe")
    ok &= check("prompt is read", tabs[2].prompt == "audit the ingest pipeline")
    ok &= check("role defaults to worker", tabs[1].role == "worker")
    ok &= check("window defaults to 1", tabs[0].window == 1)
    ok &= check("window is read", tabs[3].window == 2)
    ok &= check("panes are read", len(tabs[3].panes) == 1)
    ok &= check("pane split is read", tabs[3].panes[0].split == "h")
    ok &= check("pane inherits an empty dir", tabs[3].panes[0].dir == "")

    ok &= check("a single-bracket table is refused by name",
                "[[dragen]]" in _err('[dragen]\nname = "x"\n'))
    ok &= check("a missing name is refused",
                "name" in _err('[[d]]\ndir = "~"\n'))
    ok &= check("an unknown key names the typo",
                "cmnd" in _err('[[d]]\nname = "x"\ncmnd = "ls"\n'))
    ok &= check("an unknown key names the tab",
                "tab 1" in _err('[[d]]\nname = "x"\ncmnd = "ls"\n'))
    ok &= check("a bad arm is refused",
                "arm" in _err('[[d]]\nname = "x"\narm = "yolo"\n'))
    ok &= check("a bad role is refused",
                "role" in _err('[[d]]\nname = "x"\nprompt = "p"\nrole = "boss"\n'))
    ok &= check("a bad split is refused",
                "split" in _err('[[d]]\nname = "x"\n'
                                'panes = [ { split = "diagonal" } ]\n'))
    ok &= check("window below 1 is refused",
                "window" in _err('[[d]]\nname = "x"\nwindow = 0\n'))
    ok &= check("a missing file is refused",
                "no config" in _err_missing())
    ok &= check("invalid TOML is refused",
                "TOML" in _err('[[d]\nname = "x"\n'))

    groups = wsconfig.group_windows(tabs)
    ok &= check("windows group in first-seen order",
                [w for w, _ in groups] == [1, 2])
    ok &= check("window 1 holds three tabs", len(groups[0][1]) == 3)

    ok &= check("RELAY_WORKSPACES overrides the default path",
                _path_with_env("/tmp/ws.toml") == "/tmp/ws.toml")
    ok &= check("the default path is under ~/.relay",
                _path_with_env(None).endswith("/.relay/workspaces.toml"))

    ok &= check("ARM_MODES matches db.ARM_REQUEST_MODES", _arm_modes_match())
    ok &= check("ROLES matches db.ROLES", _roles_match())

    ok = _round_trip(ok)
    ok = _snapshot_checks(ok)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _err_missing():
    try:
        wsconfig.load("/nonexistent/dir/workspaces.toml")
        return ""
    except wsconfig.ConfigError as exc:
        return str(exc)


def _path_with_env(value):
    old = os.environ.get("RELAY_WORKSPACES")
    if value is None:
        os.environ.pop("RELAY_WORKSPACES", None)
    else:
        os.environ["RELAY_WORKSPACES"] = value
    try:
        return wsconfig.default_path()
    finally:
        if old is None:
            os.environ.pop("RELAY_WORKSPACES", None)
        else:
            os.environ["RELAY_WORKSPACES"] = old


def _arm_modes_match():
    """db is imported HERE, never at module scope: wsconfig.py must stay
    importable without sqlite3, and this test is the only reason to look."""
    import db
    return tuple(wsconfig.ARM_MODES) == tuple(db.ARM_REQUEST_MODES)


def _roles_match():
    import db
    return tuple(wsconfig.ROLES) == tuple(db.ROLES)


def _round_trip(ok):
    """render() must produce TOML that load() reads back identically."""
    _, spaces = wsconfig.load(_write(FULL))
    tabs = spaces["dragen"]
    text = wsconfig.render("dragen", tabs)
    _, again = wsconfig.load(_write(text))
    ok &= check("render round-trips through load", again["dragen"] == tabs)
    ok &= check("render omits an unset arm",
                "arm" not in text.split("[[dragen]]")[1])
    ok &= check("render keeps a set arm", 'arm = "safe"' in text)
    ok &= check("render keeps panes", "panes = [" in text)
    ok &= check("render collapses $HOME to ~", '"~/Work/dragen"' in text)

    quoted = [wsconfig.Tab(name='say "hi"', dir="/tmp",
                             cmd='echo \\ "q"')]
    _, back = wsconfig.load(_write(wsconfig.render("q", quoted)))
    ok &= check("quotes and backslashes round-trip", back["q"] == quoted)

    stripped = wsconfig.strip_block(FULL, "dragen")
    ok &= check("strip_block drops the workspace",
                "[[dragen]]" not in stripped)
    ok &= check("strip_block keeps other tables",
                "[settings]" in stripped and "warmup" in stripped)

    path = _write(FULL)
    raised = ""
    try:
        wsconfig.save(path, "dragen", tabs)
    except wsconfig.ConfigError as exc:
        raised = str(exc)
    ok &= check("save refuses to clobber without force", "force" in raised)

    wsconfig.save(path, "dragen", tabs[:1], force=True)
    _, after = wsconfig.load(path)
    ok &= check("save --force replaces the workspace",
                len(after["dragen"]) == 1)
    ok &= check("save --force keeps settings",
                after is not None and wsconfig.load(path)[0].get("warmup") == 2.0)

    path2 = _write(FULL)
    wsconfig.save(path2, "other", tabs[:1])
    _, both = wsconfig.load(path2)
    ok &= check("save appends a new workspace",
                sorted(both) == ["dragen", "other"])

    ok &= check("save creates a missing file",
                _save_into_new_file())

    path3 = _write(FULL)
    ok &= check("remove reports a hit", wsconfig.remove(path3, "dragen"))
    ok &= check("remove drops it", "dragen" not in wsconfig.load(path3)[1])
    ok &= check("remove reports a miss",
                wsconfig.remove(path3, "nosuch") is False)

    # FINDING 1: header with trailing comment
    commented = '''[settings]
warmup = 2.0

[[dragen]] # laptop tabs
name = "monitor"
dir = "~/Work/dragen"
cmd = "top"
'''
    path4 = _write(commented)
    one_tab = [wsconfig.Tab(name="new", dir="/tmp")]
    wsconfig.save(path4, "dragen", one_tab, force=True)
    _, result = wsconfig.load(path4)
    ok &= check("save with force replaces despite trailing comment",
                result["dragen"] == one_tab)

    # FINDING 2: role without prompt
    role_no_prompt = [wsconfig.Tab(name="c", dir="/tmp", role="coordinator")]
    rendered = wsconfig.render("x", role_no_prompt)
    _, loaded = wsconfig.load(_write(rendered))
    ok &= check("role without prompt survives round-trip",
                loaded["x"][0].role == "coordinator")

    # FINDING 4: single-quoted header
    single_quoted = '''[['dragen']]
name = "a"
dir = "~"
'''
    stripped_sq = wsconfig.strip_block(single_quoted, "dragen")
    ok &= check("strip_block matches single-quoted header",
                "[[" not in stripped_sq)

    # FINDING 3: atomicity
    path5 = _write(FULL)
    wsconfig.save(path5, "atomic", [wsconfig.Tab(name="t", dir="/tmp")])
    ok &= check("save does not leave .tmp file", not os.path.exists(path5 + ".tmp"))
    _, atomic_check = wsconfig.load(path5)
    ok &= check("save result still parses", "atomic" in atomic_check)

    # Prefix case: removing "dev" should not affect "devtools"
    prefix_file = '''[[dev]]
name = "x"
dir = "~"

[[devtools]]
name = "y"
dir = "~"
'''
    path6 = _write(prefix_file)
    wsconfig.remove(path6, "dev")
    _, prefix_result = wsconfig.load(path6)
    ok &= check("remove does not match name prefixes",
                "dev" not in prefix_result and "devtools" in prefix_result)

    return ok


def _save_into_new_file():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sub", "workspaces.toml")
    wsconfig.save(p, "x", [wsconfig.Tab(name="a", dir="/tmp")])
    return "x" in wsconfig.load(p)[1]


def _snapshot_checks(ok):
    rows = [
        {"name": "DRAGEN DOCS", "dir": "/Users/x/Work/dragen",
         "arm": "safe", "window": 1},
        {"name": "monitor", "dir": "/Users/x/Work/dragen",
         "arm": "", "window": 1},
        {"name": "logs", "dir": "/Users/x/Work/dragen",
         "arm": "", "window": 2},
    ]
    tabs = wsconfig.snapshot(rows)
    ok &= check("snapshot keeps every row", len(tabs) == 3)
    ok &= check("snapshot carries the name", tabs[0].name == "DRAGEN DOCS")
    ok &= check("snapshot carries the arm", tabs[0].arm == "safe")
    ok &= check("snapshot carries the window", tabs[2].window == 2)
    ok &= check("snapshot leaves cmd empty - a running tab cannot report it",
                tabs[0].cmd == "")
    ok &= check("snapshot tolerates a missing arm key",
                wsconfig.snapshot([{"name": "a", "dir": "/tmp"}])[0].arm == "")

    build, skipped = wsconfig.skip_live(tabs, {"monitor"})
    ok &= check("skip_live drops a live name", [t.name for t in build]
                == ["DRAGEN DOCS", "logs"])
    ok &= check("skip_live reports what it dropped", skipped == ["monitor"])
    build, skipped = wsconfig.skip_live(tabs, set())
    ok &= check("skip_live with nothing live builds everything",
                len(build) == 3 and skipped == [])
    build, skipped = wsconfig.skip_live(tabs, {"DRAGEN DOCS", "monitor",
                                                 "logs"})
    ok &= check("skip_live with everything live builds nothing",
                build == [] and len(skipped) == 3)

    text = wsconfig.plan_text("dragen", tabs,
                                missing_dirs={"/Users/x/Work/dragen"},
                                skipped=["monitor"])
    ok &= check("plan_text names the workspace", "dragen" in text)
    ok &= check("plan_text groups by window",
                "window 1" in text and "window 2" in text)
    ok &= check("plan_text flags a missing directory", "missing" in text)
    ok &= check("plan_text reports skipped names",
                "monitor" in text and "already live" in text)
    ok &= check("plan_text marks a supervised tab", "safe" in text)

    ok &= check("snapshot drops a row with no name key",
                len(wsconfig.snapshot([{"dir": "/tmp"}, {"name": "a", "dir": "/tmp"}])) == 1)
    nameless_result = wsconfig.snapshot([{"dir": "/tmp"}, {"name": "a", "dir": "/tmp"}])
    ok &= check("snapshot surviving tabs have names",
                all(t.name for t in nameless_result) and nameless_result[0].name == "a")

    ok &= check("snapshot drops a row with blank name",
                len(wsconfig.snapshot([{"name": "   ", "dir": "/tmp"}, {"name": "b", "dir": "/tmp"}])) == 1)
    blank_result = wsconfig.snapshot([{"name": "   ", "dir": "/tmp"}, {"name": "b", "dir": "/tmp"}])
    ok &= check("snapshot surviving tabs after blank name drop",
                len(blank_result) == 1 and blank_result[0].name == "b")

    none_dir_result = wsconfig.snapshot([{"name": "x", "dir": None}])
    ok &= check("snapshot with dir: None produces expanded home, not literal None",
                none_dir_result[0].dir == os.path.expanduser("~") and "None" not in none_dir_result[0].dir)

    rows_with_nameless = [
        {"name": "a", "dir": "/tmp"},
        {"name": "", "dir": "/tmp"},
        {"name": "b", "dir": "/tmp"},
    ]
    snapshot_tabs = wsconfig.snapshot(rows_with_nameless)
    rendered = wsconfig.render("test", snapshot_tabs)
    _, reloaded = wsconfig.load(_write(rendered))
    ok &= check("snapshot output round-trips even with nameless rows in input",
                len(reloaded["test"]) == 2 and
                [t.name for t in reloaded["test"]] == ["a", "b"])

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
