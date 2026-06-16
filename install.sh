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
exit 0
