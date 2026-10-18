#!/usr/bin/env bash
# Issue the SIP hostname's certificate with acme.sh over DNS-01.
#
# DNS-01 and not HTTP-01 on purpose: it never needs port 80 open, and the SIP
# host usually has no web server at all. The cost is a DNS API token, which is
# what this script asks .env for.
#
# The certificate is a hard dependency, not a nicety: the provider's trunk uses
# TLS, and a self-signed or wrong-name cert fails the handshake. The failure is
# silent at the call level - the trunk just 401s or the INVITE never lands.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "no .env"; exit 1; }
# Read only the three values this needs, so a stray space elsewhere in .env
# cannot break the script.
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
SIP_DOMAIN="$(get SIP_DOMAIN)"; ACME_EMAIL="$(get ACME_EMAIL)"
CF_TOKEN="$(get CLOUDFLARE_API_TOKEN)"

[ -n "$SIP_DOMAIN" ] || { echo "SIP_DOMAIN is empty in .env"; exit 1; }
[ -n "$ACME_EMAIL" ] || { echo "ACME_EMAIL is empty in .env (the ACME account contact)"; exit 1; }
[ -n "$CF_TOKEN" ] || {
  echo "CLOUDFLARE_API_TOKEN is empty in .env."
  echo "DNS-01 needs a token with Zone:DNS:Edit on ${SIP_DOMAIN}'s zone."
  echo "Create one at https://dash.cloudflare.com/profile/api-tokens"
  exit 1
}

ACME="$HOME/.acme.sh/acme.sh"
if [ ! -x "$ACME" ]; then
  echo "installing acme.sh for $ACME_EMAIL"
  curl -fsS https://get.acme.sh | sh -s "email=${ACME_EMAIL}"
fi

# acme.sh reads the Cloudflare token from CF_Token (not a --flag).
export CF_Token="$CF_TOKEN"

echo "issuing a certificate for ${SIP_DOMAIN} (DNS-01, EC-256)"
"$ACME" --issue --dns dns_cf -d "$SIP_DOMAIN" --keylength ec-256 --server letsencrypt

mkdir -p certs && chmod 700 certs
# --fullchain-file, not the bare leaf: the sip container needs the intermediate
# to present a complete chain, and some carriers reject a chainless certificate.
"$ACME" --install-cert -d "$SIP_DOMAIN" --ecc \
  --fullchain-file "$PWD/certs/sip.crt" \
  --key-file "$PWD/certs/sip.key" \
  --reloadcmd "docker restart livekit-sip"
chmod 644 certs/sip.crt
chmod 600 certs/sip.key

echo
echo "certificate installed at certs/sip.crt (+ key, mode 600)."
echo "acme.sh installs its own cron entry for renewal; the reload hook restarts"
echo "livekit-sip so a renewed cert is picked up without intervention."
echo "Next: bash bootstrap/10-stack.sh"
