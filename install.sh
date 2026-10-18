#!/usr/bin/env bash
# Install the whole thing, in dependency order, stopping at the first failure.
#
# Each step is a gate rather than a script: it verifies the layer below it before
# moving on, because every failure mode here is silent - a stack that is "up" with
# no trunk, a trunk with credentials that 401, a worker that never registers, a
# number that rings and carries no audio. Failing loudly at the right layer is the
# whole point.
#
# Usage:
#   bash install.sh            # run every gate, stop at the first failure
#   bash install.sh --check    # run no gate, just verify an existing install
#
# Nothing here prints a credential.
set -uo pipefail
cd "$(dirname "$0")"
REPO="$PWD"

step() {
  echo
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
}

if [ "${1:-}" = "--check" ]; then
  step "verifying the existing install (no changes)"
  python3 bootstrap/00-preflight.py
  step "the ladder"
  venv/bin/python bootstrap/90-check.py
  exit $?
fi

step "gate 0/5  preflight: config, DNS, certificate, ports, provider line"
python3 bootstrap/00-preflight.py || { echo; echo "Preflight failed. Nothing was started."; exit 1; }

step "gate 1/5  the LiveKit stack"
bash bootstrap/10-stack.sh || exit 1

step "gate 2/5  inbound trunk and dispatch rule"
# Uses the venv if it already exists (so a re-run after 40-agent.sh is clean),
# otherwise the system python - these scripts only need livekit-api.
if [ -x venv/bin/python ]; then
  venv/bin/python bootstrap/20-sip-objects.py || exit 1
else
  python3 bootstrap/20-sip-objects.py || {
    echo "This step needs livekit-api. Run bash bootstrap/40-agent.sh first to build the"
    echo "venv, then re-run: python3 bootstrap/20-sip-objects.py"
    exit 1
  }
fi

step "gate 3/5  provider: credentials, endpoints, inbound destination"
if [ -x venv/bin/python ]; then
  venv/bin/python bootstrap/30-photon.py || exit 1
else
  python3 bootstrap/30-photon.py || exit 1
fi

step "gate 4/5  the voice worker"
bash bootstrap/40-agent.sh || exit 1

step "gate 5/5  the dialer"
bash bootstrap/50-dialer.sh || exit 1

step "the ladder"
venv/bin/python bootstrap/90-check.py || exit 1

cat <<'MSG'

All gates passed. What is verified so far: the stack, the trunk, the provider
wiring and the worker - with no call placed and nothing billed.

The last rung costs money and needs a real phone. Do it deliberately:

  1. a room with no telephony (free):
       venv/bin/python tests/room_test.py
  2. one real call to your own number:
       venv/bin/python cli/call_cli.py --to <your number> --mode callme --instruction 'say hello'
  3. then the chat commands, if you use Hermes:
       bash surface/hermes-plugin/install.sh

If any of those fail, docs/TROUBLESHOOTING.md maps the symptom to the layer that
caused it.
MSG
