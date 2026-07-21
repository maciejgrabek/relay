#!/bin/bash
# Relay setup check. Verifies relay's prerequisites and, if bin/ isn't on
# your PATH, offers to add it to your shell rc. It never installs packages or
# edits anything else - the only change it can make is appending one PATH line,
# and only after you say yes.
#
#   ./install.sh           # check prerequisites, offer to add bin to PATH
#   ./install.sh --check   # check only, never edit anything
#
# To take Relay back off your PATH, run ./uninstall.sh.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$REPO/bin"
MARKER="# relay"

CHECK_ONLY=0
case "${1:-}" in
  --check)   CHECK_ONLY=1 ;;
  -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "" )       ;;
  *)         echo "unknown arg: $1"; exit 1 ;;
esac

bad=0
pass(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail(){ printf '  \033[31m✗\033[0m %s\n' "$1"; bad=$((bad+1)); }

echo "Relay prerequisites:"

if command -v python3 >/dev/null 2>&1; then
  pass "python3 ($(python3 --version 2>&1 | awk '{print $2}'))"
else
  fail "python3 not found -> install Python 3"
fi

for mod in iterm2 textual; do
  if python3 -c "import $mod" >/dev/null 2>&1; then
    pass "$mod module"
  else
    fail "$mod not installed -> pip install $mod"
  fi
done

on_path=0
case ":$PATH:" in *":$BIN:"*) on_path=1 ;; esac
if [ "$on_path" = 1 ]; then
  pass "bin on PATH"
else
  fail "bin not on PATH"
fi

echo
echo "iTerm2: enable the Python API once at"
echo "  Settings -> General -> Magic -> Enable Python API  (this script can't check it)"

LINE="export PATH=\"$BIN:\$PATH\" $MARKER"
if [ "$on_path" = 1 ]; then
  : # nothing to do
elif [ "$CHECK_ONLY" = 1 ]; then
  echo
  echo "To add bin to PATH, append to your shell rc:"
  echo "  $LINE"
else
  case "${SHELL:-}" in
    *bash) RC="$HOME/.bashrc" ;;
    *)     RC="$HOME/.zshrc" ;;
  esac
  echo
  printf 'Add bin to PATH in %s now? [y/N] ' "$RC"
  read -r ans
  case "$ans" in
    y|Y|yes|YES)
      printf '\n%s\n' "$LINE" >> "$RC"
      echo "✓ added to $RC - run: source $RC"
      ;;
    *)
      echo "Skipped. To do it yourself, append to your shell rc:"
      echo "  $LINE"
      ;;
  esac
fi

echo
if [ "$bad" -eq 0 ]; then
  echo "All set - run: relay --dry-run"
else
  echo "$bad item(s) need attention above."
fi

# --- update check ------------------------------------------------------------
# The TUI self-updates at start (throttled daily, ff-only); this is the same
# check, run eagerly so a fresh install starts current.
if [ "$CHECK_ONLY" != 1 ]; then
  echo
  read -r -p "Check for a newer relay now? [y/N] " up_ans
  case "$up_ans" in
    y|Y|yes|YES) python3 "$REPO/iterm/cli.py" update || true ;;
    *) echo "Skipped (the TUI checks once a day at start anyway)." ;;
  esac
fi

# --- Claude Code skills (worker/coordinator protocol) -----------------------
SKILLS_SRC="$REPO/skills"
SKILLS_DST="$HOME/.claude/skills"
echo
echo "Relay ships Claude Code skills (relay-worker, relay-coordinator)."
if [ "$CHECK_ONLY" = 1 ]; then
  echo "To symlink them, run ./install.sh (without --check) and answer yes."
else
  read -r -p "Symlink them into $SKILLS_DST? [y/N] " skills_ans
  case "$skills_ans" in
    y|Y|yes|YES)
      mkdir -p "$SKILLS_DST"
      for s in relay-worker relay-coordinator; do
        ln -sfn "$SKILLS_SRC/$s" "$SKILLS_DST/$s"
        echo "  linked $SKILLS_DST/$s"
      done
      # the shared reference sits next to the skill dirs, resolved via the
      # symlink's real path; link it too so ../relay-cli-reference.md
      # resolves either way
      ln -sfn "$SKILLS_SRC/relay-cli-reference.md" "$SKILLS_DST/relay-cli-reference.md"
      echo "  linked $SKILLS_DST/relay-cli-reference.md"
      ;;
    *)
      echo "Skipped."
      ;;
  esac
fi

# --- iTerm2 status-bar provider (AutoLaunch) --------------------------------
# iTerm2 keeps a configured status-bar component in the profile even when the
# script providing it is gone, and renders that as an ERROR. So the badge
# needs an always-on provider that iTerm2 itself launches; with relay off it
# just shows "RELAY: off". Symlinked, so `relay update` updates it too.
AL_SRC="$REPO/iterm/statusbar_autolaunch.py"
AL_DST="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
echo
echo "The status-bar badge ([statusbar] enabled) needs an always-on iTerm2"
echo "AutoLaunch provider - without it the badge slot ERRORS while relay is off."
if [ "$CHECK_ONLY" = 1 ]; then
  echo "To symlink it, run ./install.sh (without --check) and answer yes."
else
  read -r -p "Symlink it into your iTerm2 AutoLaunch scripts? [y/N] " al_ans
  case "$al_ans" in
    y|Y|yes|YES)
      mkdir -p "$AL_DST"
      ln -sfn "$AL_SRC" "$AL_DST/relay_statusbar.py"
      echo "  linked $AL_DST/relay_statusbar.py"
      echo "  start it once: iTerm2 menu Scripts > AutoLaunch >"
      echo "  relay_statusbar.py (or just restart iTerm2)."
      ;;
    *)
      echo "Skipped. (Fine if you don't use the status-bar badge.)"
      ;;
  esac
fi

exit 0
