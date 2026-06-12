#!/usr/bin/env bash
# Sync collector scripts from the agentboard monorepo (source of truth)
# into this public repo. Extracts from a git ref (default origin/main), so
# whatever branch the monorepo worktree has checked out doesn't matter.
#   ./scripts/sync-from-monorepo.sh [path-to-monorepo] [git-ref]
set -euo pipefail

MONOREPO="${1:-$HOME/Projects/agentboard}"
REF="${2:-origin/main}"
DST="$(cd "$(dirname "$0")/.." && pwd)/collectors"

FILES=(
  collect.py
  collect_codex.py
  collect_gemini.py
  collect_claude_cowork.py
  collect_opencode.py
  collect_openclaw.py
  hook.sh
)

git -C "$MONOREPO" fetch origin main -q 2>/dev/null || echo "(fetch failed — using local $REF)"

echo "Syncing $REF from $MONOREPO -> $DST"
for f in "${FILES[@]}"; do
  if git -C "$MONOREPO" show "$REF:apps/web/public/$f" > "$DST/$f.tmp" 2>/dev/null; then
    mv "$DST/$f.tmp" "$DST/$f"
    echo "  synced  $f"
  else
    rm -f "$DST/$f.tmp"
    echo "  MISSING $f on $REF — skipped"
  fi
done

echo
echo "Versions in this repo:"
grep -H "AGENTBOARD_SCRIPT_RELEASE\s*=" "$DST"/collect*.py | sed 's/.*collectors\//  /' || true
echo
echo "Reminder: if any uploaded field changed, update PRIVACY.md and docs/data-fields.md in the same commit."
