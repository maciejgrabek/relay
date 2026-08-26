"""Tests for ~/.relay/workspaces.toml. Temp files, no iTerm2 or sqlite imports.

Run: python3 iterm/test_workspaces.py    or    ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import workspaces  # noqa: E402


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
        workspaces.load(_write(text))
        return ""
    except workspaces.ConfigError as exc:
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

    settings, spaces = workspaces.load(_write(FULL))
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

    groups = workspaces.group_windows(tabs)
    ok &= check("windows group in first-seen order",
                [w for w, _ in groups] == [1, 2])
    ok &= check("window 1 holds three tabs", len(groups[0][1]) == 3)

    ok &= check("RELAY_WORKSPACES overrides the default path",
                _path_with_env("/tmp/ws.toml") == "/tmp/ws.toml")
    ok &= check("the default path is under ~/.relay",
                _path_with_env(None).endswith("/.relay/workspaces.toml"))

    ok &= check("ARM_MODES matches db.ARM_REQUEST_MODES", _arm_modes_match())
    ok &= check("ROLES matches db.ROLES", _roles_match())

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _err_missing():
    try:
        workspaces.load("/nonexistent/dir/workspaces.toml")
        return ""
    except workspaces.ConfigError as exc:
        return str(exc)


def _path_with_env(value):
    old = os.environ.get("RELAY_WORKSPACES")
    if value is None:
        os.environ.pop("RELAY_WORKSPACES", None)
    else:
        os.environ["RELAY_WORKSPACES"] = value
    try:
        return workspaces.default_path()
    finally:
        if old is None:
            os.environ.pop("RELAY_WORKSPACES", None)
        else:
            os.environ["RELAY_WORKSPACES"] = old


def _arm_modes_match():
    """db is imported HERE, never at module scope: workspaces.py must stay
    importable without sqlite3, and this test is the only reason to look."""
    import db
    return tuple(workspaces.ARM_MODES) == tuple(db.ARM_REQUEST_MODES)


def _roles_match():
    import db
    return tuple(workspaces.ROLES) == tuple(db.ROLES)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
