#!/usr/bin/env python3
"""Gate 3: the provider side - credentials, endpoints, and the inbound destination.

Four steps, each idempotent:

  1. PUT /outbound on the voice profile  -> the provider issues SBC endpoints and
     a digest credential for us, which is stored in .env
  2. create or update the LiveKit outbound trunk pointing at the strongest
     endpoint the provider offered (tls > tcp > udp)
  3. PUT /inbound on the profile -> point the provider at this host's SIP TLS
     listener, with the credentials bootstrap/20-sip-objects.py generated
  4. read the trunk back and compare its stored credential with .env

Step 4 is not ceremony: a mismatch 401s every call silently, and the trunk is the
only place the value is visible. Nothing here prints a password - lengths only.

    python3 bootstrap/30-photon.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()

from livekit.api import LiveKitAPI  # noqa: E402
from livekit.protocol import sip as psip  # noqa: E402

ENV_PATH = REPO / ".env"
PHOTON_BASE = "https://api.photon.codes"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
OUT_TRUNK_NAME = env("SIP_OUTBOUND_TRUNK", "photon-outbound")
TRANSPORT = {
    "udp": psip.SIPTransport.SIP_TRANSPORT_UDP,
    "tcp": psip.SIPTransport.SIP_TRANSPORT_TCP,
    "tls": psip.SIPTransport.SIP_TRANSPORT_TLS,
}
# Prefer an encrypted endpoint when the provider offers several.
TRANSPORT_RANK = {"tls": 0, "tcp": 1, "udp": 2}


def store_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.is_file() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                lines[i] = f"{key}={updates[key]}"
                seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)


def http_hint(status: int) -> str:
    """What a status means *for this provider*, in one line.

    Measured against api.photon.codes: a valid key with no User-Agent gets 403
    (their edge blocks bare clients), a key they do not accept gets 401, and a
    wrong project or profile id gets 404. Naming the wrong one sends you chasing
    a credential bug that is not there.
    """
    if status == 401:
        return "  (401: PHOTON_API_KEY is missing, expired, or not accepted)"
    if status == 403:
        return ("  (403 from this provider: the request carried no User-Agent, which its "
                "edge blocks - this script sends one, so check for a proxy stripping it)")
    if status == 404:
        return ("  (404: no such route - PHOTON_PROJECT_ID or PHOTON_VOICE_PROFILE_ID is "
                "wrong, or the profile was deleted)")
    if status == 422:
        return ("  (422: an id is malformed - wrong prefix or length, re-copy the project "
                "and profile ids from the dashboard)")
    if status == 409:
        return ("  (409: the profile changed since we read it - expectedVersion is stale, "
                "just re-run this gate)")
    return ""


def provider(method: str, path: str, body=None, idem: str | None = None):
    key = env("PHOTON_API_KEY")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PHOTON_BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    # Mutating calls require an idempotency key, and reusing one across retries is
    # what keeps a retry from issuing a second credential.
    req.add_header("Idempotency-Key", idem or uuid.uuid4().hex)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def best_endpoint(endpoints: list[dict]) -> dict | None:
    def rank(ep: dict) -> int:
        return TRANSPORT_RANK.get(str(ep.get("transport") or "udp").lower(), 9)

    return sorted(endpoints, key=rank)[0] if endpoints else None


def parse_stored_endpoints(raw: str) -> list[dict]:
    """Rebuild the endpoint list from the compact form kept in .env."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "/" not in item or ":" not in item:
            continue
        hostport, transport = item.rsplit("/", 1)
        host, _, port = hostport.rpartition(":")
        out.append({"host": host, "port": int(port or 5061), "transport": transport})
    return out


async def main() -> int:
    required = ("PHOTON_API_KEY", "PHOTON_PROJECT_ID", "PHOTON_VOICE_PROFILE_ID", "SIP_DOMAIN")
    missing = [k for k in required if not env(k)]
    if missing:
        print(f"missing from .env: {', '.join(missing)}")
        return 2

    project, profile_id = env("PHOTON_PROJECT_ID"), env("PHOTON_VOICE_PROFILE_ID")
    profile_path = f"/v1/projects/{project}/platforms/voice/profiles/{profile_id}"

    status, profile = provider("GET", profile_path)
    if status != 200:
        print(f"GET profile failed [{status}]: {profile}")
        print("  a 404 here usually means PHOTON_VOICE_PROFILE_ID is wrong or the project "
              "has no voice profile yet")
        return 1
    version = profile["version"]
    line_count = profile.get("lineCount")
    print(f"profile {profile_id}  version={version}  lineCount={line_count}")
    if not line_count:
        # Nothing below can work: the provider will not carry a call for a project
        # with no voice-eligible line, and no script can create one.
        print("FAIL: lineCount is 0 - this project has no voice-eligible line. Get a number "
              "in the provider dashboard before continuing.")
        return 1

    # ---- 1. outbound credential --------------------------------------------
    stored = parse_stored_endpoints(env("SIP_OUTBOUND_ALL_ENDPOINTS"))
    if env("SIP_OUTBOUND_PASS") and stored:
        print("\n=== 1. outbound: reusing the stored credential ===")
        user, password = env("SIP_OUTBOUND_USER"), env("SIP_OUTBOUND_PASS")
        realm = env("SIP_OUTBOUND_REALM")
    else:
        print("\n=== 1. PUT /outbound (algorithm sha-256) ===")
        idem = env("SIP_OUTBOUND_IDEMPOTENCY_KEY") or uuid.uuid4().hex
        store_env({"SIP_OUTBOUND_IDEMPOTENCY_KEY": idem})
        status, body = provider(
            "PUT", profile_path + "/outbound",
            {"authentication": {"algorithm": "sha-256"}, "expectedVersion": version},
            idem=idem)
        print(f"  HTTP {status}")
        if status not in (200, 201):
            print(f"  body: {body}")
            if http_hint(status):
                print(http_hint(status))
            return 1
        out = body["outbound"]
        conn = out["connection"]
        stored = conn.get("endpoints") or []
        realm = conn.get("realm") or ""
        user = out["authentication"]["username"]
        password = body["password"]
        version = body.get("profileVersion", version)
        print(f"  realm={realm!r} endpoints={len(stored)}")
        for ep in stored:
            print(f"    {ep.get('host')}:{ep.get('port')} transport={ep.get('transport')}")
        print(f"  auth user={user!r} password_len={len(password or '')}")

    primary = best_endpoint(stored)
    if not primary:
        print("  no usable endpoint returned by the provider")
        return 1
    host = primary["host"]
    port = int(primary.get("port") or 5061)
    transport = str(primary.get("transport") or "tls").lower()

    store_env({
        "SIP_OUTBOUND_HOST": host,
        "SIP_OUTBOUND_PORT": str(port),
        "SIP_OUTBOUND_TRANSPORT": transport,
        "SIP_OUTBOUND_USER": user,
        "SIP_OUTBOUND_PASS": password,
        "SIP_OUTBOUND_REALM": realm,
        "SIP_OUTBOUND_ALL_ENDPOINTS": ",".join(
            f"{e['host']}:{e.get('port')}/{e.get('transport')}" for e in stored),
    })
    print(f"  selected endpoint {host}:{port}/{transport} (stored in .env)")

    # ---- 2. LiveKit outbound trunk -----------------------------------------
    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        trunks = await api.sip.list_outbound_trunk(psip.ListSIPOutboundTrunkRequest())
        existing = next((t for t in trunks.items if t.name == OUT_TRUNK_NAME), None)
        address = f"{host}:{port}"
        transport_enum = TRANSPORT.get(transport, psip.SIPTransport.SIP_TRANSPORT_TLS)
        numbers = [env("SIP_INBOUND_NUMBER")] if env("SIP_INBOUND_NUMBER") else []

        print(f"\n=== 2. LiveKit outbound trunk -> {address} ({transport}) ===")
        if not numbers:
            print("  WARNING: no caller-ID number in .env (SIP_INBOUND_NUMBER). LiveKit wants "
                  "the trunk to own a number; most providers still accept the call, but the "
                  "callee may see 'unknown'.")
        if existing:
            # Same trap as the inbound trunk: this server answers 400 "unsupported
            # list update operation" for an *empty* list field, so only send numbers
            # when there is one - an unset SIP_INBOUND_NUMBER would otherwise break
            # every re-run of this gate.
            lists = {"numbers": numbers} if numbers else {}
            update = getattr(api.sip, "update_outbound_trunk_fields", None) \
                or api.sip.update_sip_outbound_trunk_fields
            updated = await update(
                trunk_id=existing.sip_trunk_id, address=address, transport=transport_enum,
                auth_username=user, auth_password=password, name=OUT_TRUNK_NAME, **lists)
            out_trunk_id = updated.sip_trunk_id
            print(f"  updated {out_trunk_id}")
        else:
            info = psip.SIPOutboundTrunkInfo(
                name=OUT_TRUNK_NAME, address=address, transport=transport_enum,
                numbers=numbers, auth_username=user, auth_password=password)
            created = await api.sip.create_outbound_trunk(
                psip.CreateSIPOutboundTrunkRequest(trunk=info))
            out_trunk_id = created.sip_trunk_id
            print(f"  created {out_trunk_id}")

        # ---- 4a. trunk vs .env ---------------------------------------------
        trunks = (await api.sip.list_outbound_trunk(psip.ListSIPOutboundTrunkRequest())).items
        trunk = next((t for t in trunks if t.sip_trunk_id == out_trunk_id), None)
        match = bool(trunk) and (trunk.auth_password or "") == password \
            and trunk.auth_username == user
        print(f"\n=== outbound credential agreement ===")
        print(f"  address      : {trunk.address if trunk else '(gone)'}")
        print(f"  transport    : {psip.SIPTransport.Name(trunk.transport) if trunk else '?'}")
        print(f"  username     : {trunk.auth_username!r}" if trunk else "  username     : ?")
        print(f"  password len : {len(trunk.auth_password or '')} (env {len(password or '')})")
        print(f"  match        : {match}")
    finally:
        await api.aclose()

    # ---- 3. provider inbound destination -----------------------------------
    inbound_user, inbound_pass = env("SIP_INBOUND_USER"), env("SIP_INBOUND_PASS")
    if not (inbound_user and inbound_pass):
        print("\nSIP_INBOUND_USER/PASS missing from .env - run bootstrap/20-sip-objects.py first")
        return 1

    dest_uri = f"sip:{env('SIP_DOMAIN')}:{env('SIP_TLS_PORT', '5061')};transport=tls"
    print(f"\n=== 3. PUT /inbound -> {dest_uri} ===")
    status, body = provider(
        "PUT", profile_path + "/inbound",
        {"destinationUri": dest_uri,
         "credentials": {"username": inbound_user, "password": inbound_pass},
         "expectedVersion": version},
        idem=env("SIP_INBOUND_IDEMPOTENCY_KEY") or uuid.uuid4().hex)
    print(f"  HTTP {status}")
    if status not in (200, 201):
        print(f"  body: {body}")
        if http_hint(status):
            print(http_hint(status))
        return 1
    inbound = body.get("inbound") or {}
    got = inbound.get("destinationUri")
    print(f"  destinationUri    : {got!r}")
    print(f"  credentials.user  : {(inbound.get('credentials') or {}).get('username')!r}")
    print(f"  profileVersion    : {body.get('profileVersion')}")

    print("\n=== summary ===")
    print(f"  livekit outbound trunk : {out_trunk_id} -> {address} ({transport})")
    print(f"  provider inbound dest  : {dest_uri}")
    print(f"  caller id number       : {env('SIP_INBOUND_NUMBER') or 'MISSING'}")
    if got != dest_uri:
        print("\nFAIL: the provider stored a different destination URI than we sent - inbound "
              "calls will not arrive here.")
        return 1
    print("\nOK: provider wiring done. Next: bash bootstrap/40-agent.sh")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
