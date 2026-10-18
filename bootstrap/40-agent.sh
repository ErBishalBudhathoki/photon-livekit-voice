#!/usr/bin/env bash
# Gate 4: build the worker venv, install it as a service, and prove it registered.
#
# The worker is what makes a room a conversation, so "the unit is active" is not
# the gate - "registered worker" in the log is. A worker that starts and cannot
# reach the LiveKit API, or whose venv has a mismatched wheel, sits in a restart
# loop while `systemctl status` reports it as running.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
umask 022

# Two ways to stay out of the way of a stack that is already running:
#   UNIT_NAME=livekit-agent-repo bash bootstrap/40-agent.sh   -> install a second
#     unit beside an existing one instead of overwriting it
#   bash bootstrap/40-agent.sh --no-unit                      -> build the venv and
#     stop before systemd, so you can run the worker in the foreground
UNIT_NAME="${UNIT_NAME:-livekit-agent}"
NO_UNIT=0
for arg in "$@"; do
  case "$arg" in
    --no-unit) NO_UNIT=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --no-unit)"; exit 2 ;;
  esac
done

# ---- 1. interpreter ---------------------------------------------------------
PY=""
for candidate in python3.10 python3.11 python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "no python3.10+ on PATH - the worker needs 3.10 or newer"; exit 1; }
echo "using $($PY -V 2>&1)"

# ---- 2. venv ----------------------------------------------------------------
# uv when available (much faster, and it resolves the pinned set cleanly);
# otherwise the stdlib venv. Either way the venv lives in the repo.
if [ ! -x venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "creating venv with uv ($PY)"
    uv venv --python "$PY" venv
  else
    echo "creating venv with $PY -m venv"
    "$PY" -m venv venv
    venv/bin/python -m pip install --quiet --upgrade pip
  fi
else
  echo "reusing the existing venv ($(venv/bin/python -V 2>&1))"
fi

echo "installing pinned requirements"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python venv/bin/python --quiet -r requirements.txt
else
  venv/bin/python -m pip install --quiet -r requirements.txt
fi

# ---- 3. transcript directory -----------------------------------------------
# The worker writes one JSON-lines file per call here, and the chat briefing is
# built from it. Created now so the first call is not the thing that discovers
# the path is unwritable.
CALL_DIR="$(grep -E '^AGENT_CALL_LOG_DIR=' .env | tail -1 | cut -d= -f2- || true)"
CALL_DIR="${CALL_DIR:-$REPO/calls}"
mkdir -p "$CALL_DIR"
echo "call transcripts: $CALL_DIR"
if [ ! -w "$CALL_DIR" ]; then
  echo "FAIL: $CALL_DIR is not writable - the worker could not record a call"
  exit 1
fi

# ---- 4. the unit ------------------------------------------------------------
if [ "$NO_UNIT" = 1 ]; then
  echo
  echo "--no-unit: venv built, systemd untouched."
  echo "Run the worker in the foreground with:"
  echo "  cd $REPO && ./venv/bin/python agent/agent.py start"
  exit 0
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
python3 bootstrap/render.py --only systemd/livekit-agent.service \
  --unit-path "$UNIT_DIR/$UNIT_NAME.service"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo "enabling linger so the worker survives logout"
  loginctl enable-linger "$USER" || echo "  (could not enable linger; run it as root if you need it)"
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME.service"

# ---- 5. registration gate ---------------------------------------------------
# "registered worker" is the only proof the worker reached the API and can be
# dispatched a job; anything earlier can still be a worker that will never ring.
echo "waiting for the worker to register with the LiveKit API"
for i in $(seq 1 40); do
  if journalctl --user -u "$UNIT_NAME.service" --since "-90s" --no-pager 2>/dev/null \
      | grep -q '"message": "registered worker"'; then
    echo "  worker registered"
    break
  fi
  if ! systemctl --user is-active --quiet "$UNIT_NAME.service"; then
    echo "FAIL: the unit is not active. Last 30 log lines:"
    journalctl --user -u "$UNIT_NAME.service" -n 30 --no-pager || true
    exit 1
  fi
  if [ "$i" = 40 ]; then
    echo "FAIL: no registration after 40s. Last 30 log lines:"
    journalctl --user -u "$UNIT_NAME.service" -n 30 --no-pager || true
    exit 1
  fi
  sleep 1
done

echo
echo "worker up ($UNIT_NAME.service). Next: bash bootstrap/50-dialer.sh"
