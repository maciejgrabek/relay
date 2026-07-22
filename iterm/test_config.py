"""Tests for the ~/.relay/config INI loader. Temp files, no iTerm2 imports.

Run: python3 iterm/test_config.py    or    ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".ini")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def run():
    ok = True
    # Hermetic: no env leakage between cases.
    for k in ("RELAY_CONFIG", "RELAY_STALE_MINUTES", "RELAY_NOTIFY_COOLDOWN"):
        os.environ.pop(k, None)

    # Missing file -> pure defaults, no warnings.
    cfg, warns = config.load("/nonexistent/relay-config")
    ok &= check("missing file -> defaults", cfg.title_style == "off"
                and cfg.stale_minutes == 10.0 and cfg.notify_cooldown == 30.0
                and cfg.alert_sound.endswith("Sosumi.aiff")
                and cfg.done_sound.endswith("Glass.aiff"))
    ok &= check("missing file -> no warnings", warns == [])

    # New sound keys: defaults present, and overridable.
    ok &= check("missing file -> new sound defaults",
                cfg.danger_sound.endswith("Basso.aiff")
                and cfg.message_sound.endswith("Tink.aiff"))
    p2 = _write("[sounds]\ndanger = /a/x.aiff\nmessage = /a/y.aiff\n")
    cfg2, _ = config.load(p2)
    ok &= check("sound keys read from file",
                cfg2.danger_sound == "/a/x.aiff"
                and cfg2.message_sound == "/a/y.aiff")
    ok &= check("unset new keys fall back to defaults, others still read",
                config.load(_write("[sounds]\ndanger = /a/z.aiff\n"))[0]
                .message_sound.endswith("Tink.aiff"))

    # Full file -> every key read.
    p = _write("[titles]\nstyle = hybrid\n"
               "[sounds]\nalert = /tmp/a.aiff\ndone = /tmp/d.aiff\n"
               "[swarm]\nstale_minutes = 5\nnotify_cooldown = 60\n")
    cfg, warns = config.load(p)
    ok &= check("full file read", cfg.title_style == "hybrid"
                and cfg.alert_sound == "/tmp/a.aiff"
                and cfg.done_sound == "/tmp/d.aiff"
                and cfg.stale_minutes == 5.0 and cfg.notify_cooldown == 60.0)
    ok &= check("full file -> no warnings", warns == [])

    # Partial file -> missing keys keep defaults.
    p = _write("[titles]\nstyle = glyphs\n")
    cfg, _ = config.load(p)
    ok &= check("partial file keeps defaults", cfg.title_style == "glyphs"
                and cfg.stale_minutes == 10.0)

    # Invalid style -> warning + off.
    p = _write("[titles]\nstyle = neon\n")
    cfg, warns = config.load(p)
    ok &= check("invalid style -> off + warning", cfg.title_style == "off"
                and any("neon" in w for w in warns))

    # Non-numeric tunable -> warning + default.
    p = _write("[swarm]\nstale_minutes = soon\n")
    cfg, warns = config.load(p)
    ok &= check("bad float -> default + warning", cfg.stale_minutes == 10.0
                and any("stale_minutes" in w for w in warns))

    # Malformed INI -> defaults + one warning, never raises.
    p = _write("this is not ini [ at all\n= = =\n")
    cfg, warns = config.load(p)
    ok &= check("malformed file -> defaults + warning",
                cfg.title_style == "off" and len(warns) >= 1)

    # Inline comments: the README's documented example line carries a trailing
    # `; ...` comment; it must parse to the value alone, no warning.
    p = _write("[titles]\n"
               "style = hybrid         ; off | glyphs | words | hybrid (default off)\n")
    cfg, warns = config.load(p)
    ok &= check("inline comment stripped from value",
                cfg.title_style == "hybrid" and warns == [])

    # Non-UTF-8 bytes -> defaults + warning, never raises.
    fd, p = tempfile.mkstemp(suffix=".ini")
    with os.fdopen(fd, "wb") as f:
        f.write(b"\xff\xfe[titles]\n")
    cfg, warns = config.load(p)
    ok &= check("non-utf8 file -> defaults + warning",
                cfg.title_style == "off" and len(warns) >= 1)

    # Env beats config for the two mirrored keys.
    p = _write("[swarm]\nstale_minutes = 5\nnotify_cooldown = 60\n")
    os.environ["RELAY_STALE_MINUTES"] = "2"
    os.environ["RELAY_NOTIFY_COOLDOWN"] = "7"
    try:
        cfg, _ = config.load(p)
        ok &= check("env beats config", cfg.stale_minutes == 2.0
                    and cfg.notify_cooldown == 7.0)
    finally:
        os.environ.pop("RELAY_STALE_MINUTES", None)
        os.environ.pop("RELAY_NOTIFY_COOLDOWN", None)

    # spawn_arm: read, validated, defaults off.
    p = _write("[swarm]\nspawn_arm = wild\n")
    cfg, warns = config.load(p)
    ok &= check("spawn_arm read", cfg.spawn_arm == "wild" and warns == [])
    p = _write("[swarm]\nspawn_arm = ludicrous\n")
    cfg, warns = config.load(p)
    ok &= check("bad spawn_arm -> off + warning", cfg.spawn_arm == "off"
                and any("ludicrous" in w for w in warns))
    cfg, _ = config.load("/nonexistent/relay-config")
    ok &= check("spawn_arm default off", cfg.spawn_arm == "off")

    # statusbar: default off; parsed as a bool; bad value -> off + warning
    ok &= check("statusbar default off", cfg.statusbar_enabled is False)
    p = _write("[statusbar]\nenabled = true\n")
    cfg, warns = config.load(p)
    ok &= check("statusbar enabled = true", cfg.statusbar_enabled is True
                and warns == [])
    p = _write("[statusbar]\nenabled = maybe\n")
    cfg, warns = config.load(p)
    ok &= check("statusbar bad value -> off + warning",
                cfg.statusbar_enabled is False
                and any("statusbar" in w for w in warns))

    # danger preset: default 'default'; validated; bad value -> warn + default
    ok &= check("danger preset default", cfg.danger_preset == "default")
    p = _write("[danger]\npreset = paranoid\n")
    cfg, warns = config.load(p)
    ok &= check("danger preset paranoid read",
                cfg.danger_preset == "paranoid" and warns == [])
    p = _write("[danger]\npreset = yolo\n")
    cfg, warns = config.load(p)
    ok &= check("bad preset -> default + warning",
                cfg.danger_preset == "default"
                and any("yolo" in w for w in warns))

    # theme: default phosphor; validated; bad value -> warn + phosphor
    ok &= check("theme default phosphor", cfg.theme == "phosphor")
    p = _write("[theme]\nname = amber\n")
    cfg, warns = config.load(p)
    ok &= check("theme amber read", cfg.theme == "amber" and warns == [])
    p = _write("[theme]\nname = hotdog\n")
    cfg, warns = config.load(p)
    ok &= check("bad theme -> phosphor + warning", cfg.theme == "phosphor"
                and any("hotdog" in w for w in warns))

    # RELAY_CONFIG env selects the path when load() gets None.
    p = _write("[titles]\nstyle = words\n")
    os.environ["RELAY_CONFIG"] = p
    try:
        cfg, _ = config.load()
        ok &= check("RELAY_CONFIG path honored", cfg.title_style == "words")
    finally:
        os.environ.pop("RELAY_CONFIG", None)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
