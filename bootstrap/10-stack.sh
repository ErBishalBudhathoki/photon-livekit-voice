#!/usr/bin/env bash
# Gate 1: render the configs, bring the stack up, and prove it is actually up.
#
# "docker compose ps says Up" is not proof: a LiveKit server with a broken redis
# address, or a sip container that cannot read its cert, both report Up while
# every call fails. The health gate at the end therefore talks to the API and
# looks for the SIP service in the logs.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

ENV_FILE=.env
[ -f "$ENV_FILE" ] || { echo "no .env - cp .env.example .env and fill it in first"; exit 1; }

# ---- 1. LiveKit keypair -----------------------------------------------------
# Generated once and kept in .env: rotating it invalidates every token and the
# sip container's own credentials, so it is never regenerated on a re-run.
if grep -qE '^LIVEKIT_API_KEY=$' "$ENV_FILE" || ! grep -q '^LIVEKIT_API_KEY=' "$ENV_FILE"; then
  KEY="LK$(openssl rand -hex 6)"
  SECRET="$(openssl rand -hex 32)"
  python3 - "$ENV_FILE" "$KEY" "$SECRET" <<'PY'
import sys
path, key, secret = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines()
updates = {"LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret}
seen = set()
for i, line in enumerate(lines):
    if "=" in line and not line.strip().startswith("#"):
        name = line.split("=", 1)[0].strip()
        if name in updates:
            lines[i] = f"{name}={updates[name]}"
            seen.add(name)
for name, value in updates.items():
    if name not in seen:
        lines.append(f"{name}={value}")
open(path, "w").write("\n".join(lines) + "\n")
PY
  chmod 600 "$ENV_FILE"
  echo "generated a new LiveKit keypair into .env (value not printed)"
else
  echo "reusing the LiveKit keypair already in .env"
fi

# ---- 2. render the configs --------------------------------------------------
# render.py refuses to leave a placeholder behind, so a missing variable stops
# here instead of becoming a container that starts and does not work.
echo "rendering configs"
python3 bootstrap/render.py --all

# ---- 3. certificates --------------------------------------------------------
mkdir -p certs
chmod 700 certs
if [ ! -f certs/sip.crt ]; then
  cat <<'MSG'
certs/sip.crt is missing. The carrier's TLS trunk needs a publicly trusted
certificate for your SIP hostname; run:

    bash bootstrap/cert-acme.sh

or drop your own cert.pem/key.pem in as certs/sip.crt and certs/sip.key
(if you bring a fullchain, use that as sip.crt - a bare leaf breaks the handshake).
MSG
  exit 1
fi
chmod 600 certs/sip.key 2>/dev/null || true

# ---- 4. start ---------------------------------------------------------------
echo "starting the stack"
docker compose up -d

# ---- 5. health gate ---------------------------------------------------------
API_PORT="$(grep -E '^LIVEKIT_API_PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2)"
API_PORT="${API_PORT:-7880}"
echo "waiting for the LiveKit API on 127.0.0.1:${API_PORT}"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/" >/dev/null 2>&1; then break; fi
  if [ "$i" = 30 ]; then
    echo "FAIL: the LiveKit API did not answer after 30s. Last 40 log lines:"
    docker compose logs --tail 40 livekit
    exit 1
  fi
  sleep 1
done
echo "  API is answering"

sleep 3
for name in livekit-redis livekit-server livekit-sip; do
  state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
  if [ "$state" != "running" ]; then
    echo "FAIL: $name is $state"
    docker compose logs --tail 40 "$name" || true
    exit 1
  fi
  echo "  $name running"
done

# A sip container that cannot parse its config or read its cert exits after a few
# seconds; catching that here is much cheaper than debugging it from a dead call.
sleep 5
restarts="$(docker inspect -f '{{.RestartCount}}' livekit-sip 2>/dev/null || echo 0)"
if [ "${restarts:-0}" -gt 0 ]; then
  echo "FAIL: livekit-sip has restarted $restarts time(s) - it is crash-looping:"
  docker compose logs --tail 40 sip
  exit 1
fi
if ! docker logs --tail 80 livekit-sip 2>&1 | grep -qiE 'sip|listening|started|tls'; then
  echo "WARN: no SIP startup line in the sip logs yet; check: docker compose logs sip"
fi

echo
echo "stack up. Next: python3 bootstrap/20-sip-objects.py"
echo "then verify: python3 bootstrap/90-check.py"
