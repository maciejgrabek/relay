#!/bin/sh
# Focus the iTerm2 session whose id is $1 - the tab running relay (or the tab a
# relay notification is about). Clicking a relay notification runs this via
# terminal-notifier's -execute, so the click jumps you straight to that session
# instead of opening the osascript host (Script Editor). Best-effort and silent:
# an unknown/closed id or an AppleScript hiccup just no-ops.
#
# The id is iTerm2's session GUID (hex + dashes), which the Python API exposes as
# Session.session_id and AppleScript exposes as `id of session` - the same value.
sid="$1"
[ -n "$sid" ] || exit 0
osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is "$sid" then
          select t
          select s
          set index of w to 1
          activate
          return
        end if
      end repeat
    end repeat
  end repeat
end tell
APPLESCRIPT
