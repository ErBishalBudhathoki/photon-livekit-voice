#!/usr/bin/env bash
# Install the secret-scan hooks in this clone. Idempotent; safe to re-run.
#
#   pre-commit  scans exactly what the commit would add   (blocks the accident)
#   pre-push    scans the tree and the whole history      (blocks the overlook)
#
# Bypass for one commit with `git commit --no-verify` - CI runs the same scanner,
# so the secret still cannot land.
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
HOOKS=$(git rev-parse --git-path hooks)
mkdir -p "$HOOKS"

if [ ! -f scripts/secret-scan.py ]; then
  echo "scripts/secret-scan.py is missing - run this from the repo root" >&2
  exit 1
fi
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 is required" >&2; exit 1; }

write_hook() {
  local name=$1 body=$2
  if [ -f "$HOOKS/$name" ] && ! grep -q "secret-scan" "$HOOKS/$name"; then
    cp "$HOOKS/$name" "$HOOKS/$name.pre-secret-scan.$(date +%s)"
    echo "  backed up the existing $name hook"
  fi
  printf '#!/bin/sh\n# installed by scripts/install-hooks.sh\n%s\n' "$body" \
    > "$HOOKS/$name"
  chmod +x "$HOOKS/$name"
  echo "  installed $name"
}

write_hook pre-commit "exec \"$PY\" \"\$(git rev-parse --show-toplevel)/scripts/secret-scan.py\" --staged"
write_hook pre-push   "exec \"$PY\" \"\$(git rev-parse --show-toplevel)/scripts/secret-scan.py\""

echo
echo "Done. Test it:"
echo "  printf 'AKIA%s\\n' 'IOSFODNN7EXAMPLE' > /tmp/leak.txt && cp /tmp/leak.txt leak.txt"
echo "  git add leak.txt && git commit -m test    # must be refused"
echo "  git restore --staged leak.txt && rm leak.txt"
