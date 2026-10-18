#!/usr/bin/env python3
"""Gate 2: the inbound side of the trunk - LiveKit objects + credential agreement.

Two halves, and the second is the one that bites:

1. Create or update the inbound trunk and the dispatch rule, and generate the
   digest credentials the provider must present. Idempotent: an object with the
   same name is updated in place, never duplicated.

2. Read the trunk back and compare those credentials against what .env says was
   handed to the provider. A mismatch means the provider gets 401 on every call,
   which at the call level looks exactly like "the number is broken" - so it is
   checked and reported as a hard failure here instead.

No secret value is printed: username in full, password as a length.

    python3 bootstrap/20-sip-objects.py

This gate imports livekit-api, so it needs the worker's virtualenv. install.sh
builds that venv before this step; by hand, run
`bash bootstrap/40-agent.sh --no-unit` once first.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()

# This gate imports livekit-api, but it runs before bootstrap/40-agent.sh in the
# documented order - so on a clean machine the system python has no livekit SDK at
# all and the gate died with a bare ModuleNotFoundError that reads like a broken
# repo. Re-exec into the venv when it exists; when it does not, say which command
# builds it.
try:
    import livekit  # noqa: F401
except ImportError:
    _venv = REPO / "venv" / "bin" / "python"
    if _venv.exists() and Path(sys.executable).resolve() != _venv.resolve():
        os.execv(str(_venv), [str(_venv), str(Path(__file__).resolve()), *sys.argv[1:]])
    print("This gate needs the worker's virtualenv (it talks to the LiveKit API).")
    print(f"Build it first:  bash {REPO}/bootstrap/40-agent.sh --no-unit")
    sys.exit(1)

from livekit.api import LiveKitAPI  # noqa: E402
from livekit.protocol import sip as psip  # noqa: E402

ENV_PATH = REPO / ".env"
IN_TRUNK_NAME = env("SIP_INBOUND_TRUNK", "photon-inbound")
RULE_NAME = env("SIP_DISPATCH_RULE", "photon-inbound")
ROOM_PREFIX = env("ROOM_PREFIX", "call-")
PHOTON_BASE = "https://api.photon.codes"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


def mask(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return ("*" * (len(d) - 4) + d[-4:]) if len(d) > 4 else "(none)"


def store_env(updates: dict[str, str]) -> None:
    """Merge into .env, preserving comments and every other line."""
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


def ensure_inbound_creds() -> tuple[str, str]:
    """The digest pair the provider authenticates with. Generated once, reused."""
    user, password = env("SIP_INBOUND_USER"), env("SIP_INBOUND_PASS")
    if user and password:
        print(f"  reusing SIP_INBOUND_USER={user} (password len {len(password)})")
        return user, password
    user = "photon-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(32)
    store_env({"SIP_INBOUND_USER": user, "SIP_INBOUND_PASS": password})
    print(f"  generated SIP_INBOUND_USER={user} (password len {len(password)}, stored in .env)")
    return user, password


def provider_number() -> str | None:
    """The provider's active voice line, straight from its API (masked in output).

    The number lives at detail.phoneNumber; the older top-level `number`/`e164`
    shapes come back None, which silently builds a trunk that owns no number.
    """
    if env("SIP_INBOUND_NUMBER"):
        print(f"  number: {mask(env('SIP_INBOUND_NUMBER'))} (from .env)")
        return env("SIP_INBOUND_NUMBER")
    key, project = env("PHOTON_API_KEY"), env("PHOTON_PROJECT_ID")
    if not (key and project):
        print("  number: PHOTON_API_KEY / PHOTON_PROJECT_ID not set, skipping lookup")
        return None
    req = urllib.request.Request(
        f"{PHOTON_BASE}/v1/projects/{project}/platforms/resources",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                 "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"  number: HTTP {exc.code} from the provider API")
        # Measured: 401 = key missing/not accepted, 403 = no User-Agent (their edge
        # blocks bare clients), 404 = wrong project id. This script sends a UA, so a
        # 403 here points at something stripping the header, not at the key.
        if exc.code == 401:
            print("  number: 401 - PHOTON_API_KEY is missing, expired, or not accepted")
        elif exc.code == 403:
            print("  number: 403 - this API blocks clients that send no User-Agent and "
                  "this script sends one, so check for a proxy stripping the header")
        elif exc.code == 404:
            print("  number: 404 - PHOTON_PROJECT_ID is wrong, or the project has no "
                  "voice resource yet")
        elif exc.code == 422:
            print("  number: 422 - PHOTON_PROJECT_ID is malformed (wrong prefix or "
                  "length) - re-copy it from the dashboard")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  number: {type(exc).__name__}: {exc}")
        return None
    for res in data.get("resources") or []:
        if res.get("type") == "voip_line" and res.get("state") == "active":
            num = (res.get("detail") or {}).get("phoneNumber")
            if num:
                print(f"  number: {mask(num)} ({res.get('resourceId')})")
                return num
    print("  number: no active voip_line on this project")
    return None


async def main() -> int:
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") if not env(k)]
    if missing:
        print(f"missing from the environment: {', '.join(missing)} - run 10-stack.sh first")
        return 2

    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        print("=== existing SIP objects ===")
        inbound = (await api.sip.list_inbound_trunk(psip.ListSIPInboundTrunkRequest())).items
        existing_trunk = None
        for t in inbound:
            print(f"  inbound trunk : {t.sip_trunk_id}  name={t.name!r}  "
                  f"numbers={list(t.numbers)}")
            if t.name == IN_TRUNK_NAME:
                existing_trunk = t
        rules = (await api.sip.list_dispatch_rule(psip.ListSIPDispatchRuleRequest())).items
        existing_rule = None
        for r in rules:
            print(f"  dispatch rule : {r.sip_dispatch_rule_id}  name={r.name!r}  "
                  f"trunks={list(r.trunk_ids)}")
            if r.name == RULE_NAME:
                existing_rule = r

        user, password = ensure_inbound_creds()
        number = provider_number()
        allowed = [a.strip() for a in env("SIP_ALLOWED_ADDRESSES").split(",") if a.strip()]

        print("\n=== inbound trunk ===")
        if existing_trunk:
            trunk_id = existing_trunk.sip_trunk_id
            # This server refuses an *empty* list field on an update: the SDK still
            # marks the oneof as "set" and the API answers 400 "unsupported list
            # update operation". Probing the live server: numbers=[n] -> 200,
            # allowed_addresses=[] -> 400. So send a list field only when it has
            # something in it - which also means an unset SIP_ALLOWED_ADDRESSES
            # leaves any existing allow-list alone instead of wiping it.
            lists = {}
            if number:
                lists["numbers"] = [number]
            if allowed:
                lists["allowed_addresses"] = allowed
            update = getattr(api.sip, "update_inbound_trunk_fields", None) \
                or api.sip.update_sip_inbound_trunk_fields
            updated = await update(
                trunk_id=trunk_id,
                auth_username=user,
                auth_password=password,
                name=IN_TRUNK_NAME,
                **lists,
            )
            print(f"  updated {trunk_id}: numbers={list(updated.numbers)} "
                  f"allowed={list(updated.allowed_addresses)}")
        else:
            info = psip.SIPInboundTrunkInfo(
                name=IN_TRUNK_NAME,
                numbers=[number] if number else [],
                allowed_addresses=allowed,
                auth_username=user,
                auth_password=password,
                # Allow rather than require: a provider that offers SRTP and one
                # that does not are both fine, a required-mode trunk is not.
                media_encryption=psip.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_ALLOW,
            )
            created = await api.sip.create_sip_inbound_trunk(
                psip.CreateSIPInboundTrunkRequest(trunk=info))
            trunk_id = created.sip_trunk_id
            print(f"  created {trunk_id}: numbers={list(created.numbers)}")

        print("\n=== dispatch rule ===")
        rule = psip.SIPDispatchRule(
            dispatch_rule_individual=psip.SIPDispatchRuleIndividual(room_prefix=ROOM_PREFIX))
        if existing_rule:
            updated_rule = await api.sip.update_sip_dispatch_rule_fields(
                rule_id=existing_rule.sip_dispatch_rule_id,
                trunk_ids=[trunk_id], rule=rule, name=RULE_NAME)
            rule_id = updated_rule.sip_dispatch_rule_id
            print(f"  updated {rule_id}: trunks={list(updated_rule.trunk_ids)} "
                  f"room_prefix={ROOM_PREFIX!r}")
        else:
            created_rule = await api.sip.create_sip_dispatch_rule(
                psip.CreateSIPDispatchRuleRequest(
                    name=RULE_NAME, trunk_ids=[trunk_id], rule=rule))
            rule_id = created_rule.sip_dispatch_rule_id
            print(f"  created {rule_id}: room_prefix={ROOM_PREFIX!r}")

        # ---- credential agreement -------------------------------------------
        # The provider was handed the .env values; the trunk holds what LiveKit
        # will actually demand. Comparing them here is the difference between a
        # five-second fix and an afternoon of "the number does not work".
        print("\n=== credential agreement (trunk vs .env) ===")
        trunks = (await api.sip.list_inbound_trunk(psip.ListSIPInboundTrunkRequest())).items
        trunk = next((t for t in trunks if t.sip_trunk_id == trunk_id), None)
        ok_user = bool(trunk) and trunk.auth_username == user
        ok_pass = bool(trunk) and (trunk.auth_password or "") == password
        print(f"  username match: {ok_user}  (trunk={trunk.auth_username!r} env={user!r})")
        print(f"  password match: {ok_pass}  "
              f"(len trunk={len(trunk.auth_password or '')} len env={len(password)})")
        owns_number = bool(trunk) and bool(list(trunk.numbers))
        print(f"  trunk owns a number: {owns_number}  {list(trunk.numbers) if trunk else []}")

        print("\n=== give the provider these ===")
        print(f"  SIP URI    : sip:{env('SIP_DOMAIN')}:{env('SIP_TLS_PORT', '5061')};transport=tls")
        print(f"  username   : {user}")
        print("  password   : the value of SIP_INBOUND_PASS in .env")
        print(f"  (bootstrap/30-photon.py pushes both, plus the destination URI, automatically)")

        if not (ok_user and ok_pass):
            print("\nFAIL: the trunk's credentials do not match .env. The provider will get 401 "
                  "and every inbound call will fail. Re-run this script, or push the .env "
                  "values to the provider again with bootstrap/30-photon.py.")
            return 1
        if not owns_number:
            print("\nWARN: the trunk owns no number. Most providers still deliver fine, but a "
                  "trunk with a number list rejects calls to a number it does not own.")
        print("\nOK: inbound trunk ready. Next: python3 bootstrap/30-photon.py")
        return 0
    finally:
        await api.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
