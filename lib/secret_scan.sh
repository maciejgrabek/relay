#!/bin/bash
# Relay - block secrets from ever entering the repo.
#
#   ./lib/secret_scan.sh --staged     scan what is about to be committed
#   ./lib/secret_scan.sh <file>...    scan specific files
#   ./lib/secret_scan.sh --install    install as .git/hooks/pre-commit
#
# Why this exists: relay's image generator reads its fal.ai key from
# ~/.relay/fal.key, OUTSIDE the repo, so there is nothing in the tree to
# accidentally commit. That design removes the likely failure. This catches the
# one it cannot: a human pasting a key into a file "just to test quickly".
#
# Exit 0 = clean, 1 = something key-shaped found (commit is refused).
# Deliberately noisy about WHERE, never about WHAT: a scanner that prints the
# secret it found has leaked it into your terminal scrollback and CI logs.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# Patterns, most specific first. Each is a shape that is almost never a
# legitimate literal in source.
#   fal.ai      <uuid>:<32+ hex>
#   openai      sk-... / sk-proj-...
#   anthropic   sk-ant-...
#   aws         AKIA + 16 upper-alnum
#   generic     a quoted 32+ char high-entropy blob assigned to a *KEY/*TOKEN
PATTERNS=(
  '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:[0-9a-fA-F]{32,}'
  'sk-ant-[A-Za-z0-9_-]{20,}'
  'sk-proj-[A-Za-z0-9_-]{20,}'
  'sk-[A-Za-z0-9]{32,}'
  'AKIA[0-9A-Z]{16}'
  '(API_?KEY|SECRET|TOKEN|PASSWORD)[[:space:]]*[=:][[:space:]]*["'"'"'][A-Za-z0-9/+_-]{32,}["'"'"']'
)

# This file necessarily contains the patterns themselves; scanning it would
# always fail. Same for the docs that explain the scheme.
SELF_EXEMPT='(lib/secret_scan\.sh|docs/specs/.*-design\.md)$'

scan_blob() {   # $1 = label shown to the human, stdin = content
  local label="$1" hits=0 p
  local content
  content="$(cat)"
  # Skip binaries. A key pasted into a PNG is not the threat model, and grepping
  # megabytes of random bytes for hex patterns invites false positives that
  # would train people to reach for --no-verify.
  case "$content" in
    *$'\x00'*) return 0 ;;
  esac
  for p in "${PATTERNS[@]}"; do
    if printf '%s' "$content" | grep -Eqs "$p"; then
      # Report the line NUMBER and pattern index only - never the match.
      local n
      n="$(printf '%s' "$content" | grep -Ensm1 "$p" | cut -d: -f1)"
      echo "  $label:${n:-?}  matches a credential pattern"
      hits=1
    fi
  done
  return $hits
}

fail=0

if [ "${1:-}" = "--install" ]; then
  hook="$REPO/.git/hooks/pre-commit"
  mkdir -p "$(dirname "$hook")"
  cat > "$hook" <<'HOOK'
#!/bin/bash
# Installed by lib/secret_scan.sh --install
exec "$(git rev-parse --show-toplevel)/lib/secret_scan.sh" --staged
HOOK
  chmod +x "$hook"
  echo "installed $hook"
  exit 0
fi

if [ "${1:-}" = "--staged" ]; then
  # Scan the staged CONTENT, not the working tree: those differ, and it is the
  # staged bytes that would land in the commit.
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s\n' "$f" | grep -Eqs "$SELF_EXEMPT" && continue
    git show ":$f" 2>/dev/null | scan_blob "$f" || fail=1
  done < <(git -C "$REPO" diff --cached --name-only --diff-filter=ACM)
else
  for f in "$@"; do
    printf '%s\n' "$f" | grep -Eqs "$SELF_EXEMPT" && continue
    [ -f "$f" ] || continue
    scan_blob "$f" < "$f" || fail=1
  done
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "REFUSED: credential-shaped content above."
  echo "Keys belong OUTSIDE the repo - relay reads fal.ai from ~/.relay/fal.key"
  echo "(mode 600). Nothing in this tree should ever hold one."
  echo "If this is a false positive, commit with --no-verify and tell someone."
  exit 1
fi
exit 0
