#!/usr/bin/env bash
# Install the chat surface: /callme, /call, /calllog + the post-call briefing.
#
# Copies the plugin into a Hermes profile and grants it the one privileged thing
# it needs - injecting a message into a live conversation. The grant is explicit
# because a plugin that can inject into any conversation is a plugin that can
# impersonate anything; Hermes refuses it by default, and it should.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

PROFILE="${HERMES_PROFILE:-default}"
if [ "$PROFILE" = "default" ]; then
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
else
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/$PROFILE}"
fi
PLUGIN_DIR="$HERMES_HOME/plugins/voice-call"

[ -d "$HERMES_HOME" ] || {
  echo "no Hermes home at $HERMES_HOME"
  echo "set HERMES_HOME (or HERMES_PROFILE) if Hermes lives somewhere else"
  exit 1
}

mkdir -p "$PLUGIN_DIR"
install -m 644 "$HERE/__init__.py" "$PLUGIN_DIR/__init__.py"
install -m 644 "$HERE/plugin.yaml" "$PLUGIN_DIR/plugin.yaml"
echo "installed the plugin into $PLUGIN_DIR"

# Tell the plugin where this repo is, so it can shell out to the dialer. Written
# into the Hermes .env rather than hardcoded, so the repo can live anywhere.
ENV_FILE="$HERMES_HOME/.env"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -q '^PHOTON_VOICE_DIR=' "$ENV_FILE" 2>/dev/null; then
  printf 'PHOTON_VOICE_DIR=%s\n' "$REPO" >> "$ENV_FILE"
  echo "set PHOTON_VOICE_DIR=$REPO in $ENV_FILE"
else
  echo "PHOTON_VOICE_DIR already set in $ENV_FILE (leaving it alone)"
fi

# The owner number /callme dials, if it is not already configured.
STACK_ENV="$REPO/.env"
if ! grep -q '^VOICE_CALL_OWNER=' "$ENV_FILE" 2>/dev/null && [ -f "$STACK_ENV" ]; then
  OWNER="$(grep -E '^OWNER_NUMBER=' "$STACK_ENV" | tail -1 | cut -d= -f2- || true)"
  if [ -n "$OWNER" ]; then
    printf 'VOICE_CALL_OWNER=%s\n' "$OWNER" >> "$ENV_FILE"
    echo "set VOICE_CALL_OWNER from the stack .env (number not printed)"
  fi
fi

# ---- the one privileged grant ----------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  hermes config set plugins.entries.voice-call.allow_gateway_injection true
  echo "granted gateway injection for voice-call"
else
  echo
  echo "The 'hermes' CLI is not on PATH. Grant the injection permission by hand,"
  echo "in $HERMES_HOME/config.yaml:"
  echo
  echo "  plugins:"
  echo "    entries:"
  echo "      voice-call:"
  echo "        allow_gateway_injection: true"
fi

cat <<MSG

Installed. Reload the gateway so the plugin is picked up (a running CLI session
does not hot-reload edited plugin code):

  hermes gateway reload

Then, in a chat:

  /callme                              call your own number, no brief needed
  /call +61400000000 ask about Saturday hours
  /calllog                             reprint the last call's transcript
  /calllog <room>                      reprint a specific one

When a call ends, the transcript is posted back into the conversation the command
came from - in Telegram, in the local CLI, wherever it was typed.
MSG
