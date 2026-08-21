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

    # mascot: default crt (the brand mark); validated; bad value -> warn + crt
    cfg, _ = config.load("/nonexistent/relay-config")
    ok &= check("mascot default crt", cfg.mascot == "crt")
    p = _write("[mascot]\nname = invader\n")
    cfg, warns = config.load(p)
    ok &= check("mascot invader read", cfg.mascot == "invader" and warns == [])
    p = _write("[mascot]\nname = Owl\n")
    cfg, warns = config.load(p)
    ok &= check("mascot name is case-insensitive", cfg.mascot == "owl")
    p = _write("[mascot]\nname = wombat\n")
    cfg, warns = config.load(p)
    ok &= check("bad mascot -> crt + warning", cfg.mascot == "crt"
                and any("wombat" in w for w in warns))

    # preview panel: default shown (True); parsed as bool; bad value -> True.
    cfg, _ = config.load("/nonexistent/relay-config")
    ok &= check("preview panel defaults to shown", cfg.preview_panel is True)
    p = _write("[layout]\npreview = off\n")
    cfg, warns = config.load(p)
    ok &= check("preview = off hides the panel",
                cfg.preview_panel is False and warns == [])
    p = _write("[layout]\npreview = maybe\n")
    cfg, warns = config.load(p)
    ok &= check("bad preview value -> shown + warning",
                cfg.preview_panel is True
                and any("preview" in w for w in warns))

    # RELAY_CONFIG env selects the path when load() gets None.
    p = _write("[titles]\nstyle = words\n")
    os.environ["RELAY_CONFIG"] = p
    try:
        cfg, _ = config.load()
        ok &= check("RELAY_CONFIG path honored", cfg.title_style == "words")
    finally:
        os.environ.pop("RELAY_CONFIG", None)

    # timers: two bools + a number; defaults; bad values warn
    cfg, _ = config.load("/nonexistent/relay-config")
    ok &= check("timers defaults",
                cfg.timers_require_armed is False
                and cfg.timers_autostart is False
                and cfg.timers_reconfirm_days == 7.0)
    p = _write("[timers]\nrequire_armed = true\nautostart = true\n"
               "reconfirm_days = 3\n")
    cfg, warns = config.load(p)
    ok &= check("timers keys parsed",
                cfg.timers_require_armed is True and cfg.timers_autostart is True
                and cfg.timers_reconfirm_days == 3.0 and warns == [])
    p = _write("[timers]\nrequire_armed = maybe\n")
    cfg, warns = config.load(p)
    ok &= check("bad timers bool -> false + warning",
                cfg.timers_require_armed is False
                and any("require_armed" in w for w in warns))
    p = _write("[timers]\nreconfirm_days = soon\n")
    cfg, warns = config.load(p)
    ok &= check("bad reconfirm_days -> default + warning",
                cfg.timers_reconfirm_days == 7.0
                and any("reconfirm_days" in w for w in warns))

    # [swarm] respect_draft: protective, so it defaults ON when the key is
    # absent from the file (and when the section exists without it - the shape
    # every already-installed ~/.relay/config has today).
    ok &= check("respect_draft defaults on (key absent)",
                config.Config().respect_draft is True
                and config.load("/nonexistent/relay-config")[0]
                .respect_draft is True
                and config.load(_write("[swarm]\nstale_minutes = 5\n"))[0]
                .respect_draft is True)
    p = _write("[swarm]\nrespect_draft = false\n")
    cfg, warns = config.load(p)
    ok &= check("respect_draft = false parsed",
                cfg.respect_draft is False and warns == [])
    p = _write("[swarm]\nrespect_draft = sometimes\n")
    cfg, warns = config.load(p)
    ok &= check("bad respect_draft -> on + warning",
                cfg.respect_draft is True
                and any("respect_draft" in w for w in warns))

    import dataclasses
    # dump -> load round-trips every managed field (non-default values).
    custom = dataclasses.replace(
        config.Config(), title_style="hybrid", sounds_enabled=False,
        alert_sound="/a/x.aiff",
        done_sound="", danger_sound="/a/d.aiff", message_sound="/a/m.aiff",
        stale_minutes=7.0, notify_cooldown=15.0, spawn_arm="wild",
        statusbar_enabled=True, danger_preset="paranoid",
        theme="amber", mascot="invader", preview_panel=False,
        respect_draft=False,
        timers_require_armed=True, timers_autostart=True,
        timers_reconfirm_days=3.0)
    p = _write(config.dump(custom))
    back, warns = config.load(p)
    ok &= check("dump->load round-trips every field", back == custom)
    ok &= check("round-trip has no warnings", warns == [])
    # save writes atomically to the given path.
    sp = os.path.join(tempfile.mkdtemp(), "cfg")
    config.save(custom, sp)
    ok &= check("save then load equals cfg", config.load(sp)[0] == custom)
    ok &= check("silent sound round-trips as empty", back.done_sound == "")

    # [sounds] enabled: defaults on, parses, and a bad value warns.
    ok &= check("sounds default to on", config.Config().sounds_enabled is True)
    p = _write("[sounds]\nenabled = false\nalert = /a/x.aiff\n")
    cfg, warns = config.load(p)
    ok &= check("mute parses and keeps the sound choice",
                cfg.sounds_enabled is False and cfg.alert_sound == "/a/x.aiff")
    p = _write("[sounds]\nenabled = loud\n")
    cfg, warns = config.load(p)
    ok &= check("bad [sounds] enabled -> on + warning",
                cfg.sounds_enabled is True
                and any("[sounds] enabled" in w for w in warns))

    # --- [power] release_after -----------------------------------------------
    import dataclasses
    ok &= check("power release_after defaults to 0 (never release)",
                config.Config().power_release_after == 0.0)

    cfg, warns = config.load(_write("[power]\nrelease_after = 45\n"))
    ok &= check("reads [power] release_after", cfg.power_release_after == 45.0)
    ok &= check("a good value warns about nothing", not warns)

    cfg, warns = config.load(_write("[power]\nrelease_after = soon\n"))
    ok &= check("a non-numeric release_after falls back to 0",
                cfg.power_release_after == 0.0)
    ok &= check("and says so", any("release_after" in w for w in warns))

    cfg, _ = config.load(_write("[power]\nrelease_after = -5\n"))
    ok &= check("a negative release_after clamps to 0",
                cfg.power_release_after == 0.0)

    pw_path = os.path.join(tempfile.mkdtemp(), "power-cfg")
    config.save(dataclasses.replace(config.Config(),
                                    power_release_after=30.0), pw_path)
    back, _ = config.load(pw_path)
    ok &= check("release_after round-trips through dump/load",
                back.power_release_after == 30.0)

    # --- [burn] window --------------------------------------------------------
    ok &= check("burn window defaults to 15 minutes",
                config.Config().burn_window == 15.0)

    cfg, warns = config.load(_write("[burn]\nwindow = 25\n"))
    ok &= check("reads [burn] window", cfg.burn_window == 25.0)
    ok &= check("a good burn value warns about nothing", not warns)

    cfg, warns = config.load(_write("[burn]\nwindow = later\n"))
    ok &= check("a non-numeric window falls back to the default",
                cfg.burn_window == 15.0)
    ok &= check("and says so", any("window" in w for w in warns))

    cfg, _ = config.load(_write("[burn]\nwindow = -5\n"))
    ok &= check("a negative window clamps to 0 (off)", cfg.burn_window == 0.0)

    bw_path = os.path.join(tempfile.mkdtemp(), "burn-cfg")
    config.save(dataclasses.replace(config.Config(), burn_window=40.0),
                bw_path)
    back, _ = config.load(bw_path)
    ok &= check("burn window round-trips through dump/load",
                back.burn_window == 40.0)

    # --- [events] section ---------------------------------------------------
    import events as _events
    ok &= check("config.POST_BODIES matches events.POST_BODIES",
                config.POST_BODIES == _events.POST_BODIES)

    d = config.Config()
    ok &= check("events_file defaults on", d.events_file is True)
    ok &= check("events_post_url defaults empty", d.events_post_url == "")
    ok &= check("events_post_body defaults minimal",
                d.events_post_body == "minimal")
    ok &= check("events_retention_days defaults 7",
                d.events_retention_days == 7.0)

    p = os.path.join(tempfile.mkdtemp(), "cfg")
    with open(p, "w") as f:
        f.write("[events]\n"
                "file = false\n"
                "post_url = https://ntfy.sh/my-fleet\n"
                "post_body = full\n"
                "retention_days = 3\n")
    cfg, warns = config.load(p)
    ok &= check("[events] file parsed", cfg.events_file is False)
    ok &= check("[events] post_url parsed",
                cfg.events_post_url == "https://ntfy.sh/my-fleet")
    ok &= check("[events] post_body parsed", cfg.events_post_body == "full")
    ok &= check("[events] retention_days parsed",
                cfg.events_retention_days == 3.0)
    ok &= check("valid [events] section warns about nothing",
                not [w for w in warns if "events" in w])

    # a '%' in a URL must not raise out of load() - ConfigParser's default
    # BasicInterpolation would, and load() promises it never raises
    with open(p, "w") as f:
        f.write("[events]\npost_url = https://ntfy.sh/t?msg=a%20b\n")
    raised = None
    try:
        cfg, warns = config.load(p)
    except Exception as exc:          # noqa: BLE001 - the point of the test
        raised = exc
    ok &= check("a percent-encoded post_url does not raise", raised is None)
    ok &= check("a percent-encoded post_url survives intact",
                raised is None and cfg.events_post_url.endswith("msg=a%20b"))

    # a non-http value (the visible symptom of truncation at a '#') is
    # disabled with a warn rather than silently POSTing nowhere
    with open(p, "w") as f:
        f.write("[events]\npost_url = ntfy.sh/no-scheme\n")
    cfg, warns = config.load(p)
    ok &= check("a non-http post_url is disabled", cfg.events_post_url == "")
    ok &= check("a non-http post_url warns",
                any("post_url" in w for w in warns))

    with open(p, "w") as f:
        f.write("[events]\npost_body = telegram\n")
    cfg, warns = config.load(p)
    ok &= check("bogus post_body falls back to minimal",
                cfg.events_post_body == "minimal")
    ok &= check("bogus post_body warns",
                any("post_body" in w for w in warns))

    # dump() round-trips every [events] field
    src = config.Config(events_file=False,
                        events_post_url="https://example.test/hook",
                        events_post_body="full",
                        events_retention_days=2.0)
    p2 = os.path.join(tempfile.mkdtemp(), "cfg2")
    with open(p2, "w") as f:
        f.write(config.dump(src))
    back, _ = config.load(p2)
    ok &= check("dump/load round-trips [events]",
                back.events_file == src.events_file
                and back.events_post_url == src.events_post_url
                and back.events_post_body == src.events_post_body
                and back.events_retention_days == src.events_retention_days)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
