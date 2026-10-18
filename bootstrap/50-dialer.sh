#!/usr/bin/env bash
# Gate 5: the dialer can see the trunk, and the numbers are usable.
#
# This is the last rung that costs nothing: it creates no room, dials no number
# and bills nobody. If it passes, the only things left to go wrong are the
# carrier and the model.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

[ -x venv/bin/python ] || { echo "no venv - run bootstrap/40-agent.sh first"; exit 1; }
[ -f cli/call_cli.py ] || { echo "cli/call_cli.py is missing"; exit 1; }

get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
OWNER="$(get OWNER_NUMBER)"
CC="$(get DEFAULT_COUNTRY_CODE)"

echo "=== configuration ==="
if [ -n "$OWNER" ]; then
  echo "  OWNER_NUMBER         : ${OWNER:0:3}****${OWNER: -4} (used by /callme)"
else
  echo "  OWNER_NUMBER         : empty - /callme will refuse to dial"
fi
if [ -n "$CC" ]; then
  echo "  DEFAULT_COUNTRY_CODE : $CC (local numbers like 0456... are expanded with it)"
else
  echo "  DEFAULT_COUNTRY_CODE : empty - numbers must be given in full E.164 form"
fi

# ---- the dialer's own view of the stack -------------------------------------
# Run it in a clean environment on purpose. The Hermes plugin does exactly this:
# inheriting a foreign PYTHONPATH makes this venv's cryptography wheel fail to
# import with "undefined symbol: PyType_GetName", which reads like a broken
# install rather than a leaked environment variable.
echo
echo "=== the dialer's view (trunks and rooms) ==="
env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP -u PYTHONEXECUTABLE -u VIRTUAL_ENV \
    -u LD_LIBRARY_PATH -u LD_PRELOAD \
    venv/bin/python cli/call_cli.py --list

echo
echo "OK: the dialer reached the LiveKit API and can see the trunk above."
echo "A trunk listed with the right address is what every real call needs."
echo
echo "Next: prove a room works with no telephony at all -"
echo "  venv/bin/python tests/room_test.py"
echo "then a real call:"
echo "  venv/bin/python cli/call_cli.py --to <number> --instruction 'say why you called'"
