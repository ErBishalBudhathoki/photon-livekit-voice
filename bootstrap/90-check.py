#!/usr/bin/env python3
"""The ladder: verify the installed stack actually works, in dependency order.

The rungs are ordered by what they unblock, and each one is a thing that has
failed silently at least once:

  1. stack      containers up, API answering, ports listening, nothing crash-looping
  2. credentials  both trunks carry exactly what .env handed the provider, the
                  dispatch rule exists, and the provider points back at this host
  3. worker     a worker is registered with the API and can write a transcript
  4. telephony  (not here - it needs a real call; see tests/room_test.py and the
                  one real call in the skill)

Everything above rung 4 is free: no room, no dial, no bill.

    python3 bootstrap/90-check.py
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()

# This script needs the worker's venv (it talks to the LiveKit API), but it is
# documented as `python3 bootstrap/90-check.py` and run by install.sh --check.
# Re-exec into the venv rather than failing with a bare ModuleNotFoundError that
# reads like a broken install.
try:
    import livekit  # noqa: F401
except ImportError:
    _venv = REPO / "venv" / "bin" / "python"
    if _venv.exists() and Path(sys.executable).resolve() != _venv.resolve():
        os.execv(str(_venv), [str(_venv), str(Path(__file__).resolve()), *sys.argv[1:]])
    print("This check needs the worker's virtualenv (it talks to the LiveKit API).")
    print(f"Build it first:  bash {REPO}/bootstrap/40-agent.sh --no-unit")
    sys.exit(1)

from livekit.api import LiveKitAPI  # noqa: E402
from livekit.protocol import sip as psip  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
rungs: dict[str, list[tuple[str, str, str]]] = {}


def record(rung: str, status: str, name: str, detail: str = "") -> None:
    rungs.setdefault(rung, []).append((status, name, detail))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def sh(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def mask(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return ("*" * (len(d) - 4) + d[-4:]) if len(d) > 4 else "(none)"


# ---------------------------------------------------------------- rung 1: stack
def rung_stack() -> None:
    print("\n[1] stack")
    for name in ("livekit-redis", "livekit-server", "livekit-sip"):
        code, out = sh("docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}}", name)
        if code != 0:
            record("1. stack", FAIL, f"{name} exists", "not found - run bash bootstrap/10-stack.sh")
            continue
        state, _, restarts = out.partition(" ")
        if state != "running":
            record("1. stack", FAIL, f"{name} running", f"state={state}")
        elif int(restarts or 0) > 0:
            record("1. stack", FAIL, f"{name} stable",
                   f"has restarted {restarts} time(s) - it is crash-looping")
        else:
            record("1. stack", PASS, f"{name} running", "0 restarts")

    port = env("LIVEKIT_API_PORT", "7880")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            record("1. stack", PASS, "LiveKit API answering", f"HTTP {r.status} on :{port}")
    except urllib.error.HTTPError as exc:
        # Any HTTP answer means the server is up; only a connection error is bad.
        record("1. stack", PASS, "LiveKit API answering", f"HTTP {exc.code} on :{port}")
    except Exception as exc:  # noqa: BLE001
        record("1. stack", FAIL, "LiveKit API answering", f"{type(exc).__name__}: {exc}")

    for key, label in (("REDIS_PORT", "state store"), ("SIP_PORT", "SIP"),
                       ("SIP_TLS_PORT", "SIP TLS")):
        raw = env(key)
        if not raw:
            continue
        sock = socket.socket()
        sock.settimeout(3)
        try:
            sock.connect(("127.0.0.1", int(raw)))
            record("1. stack", PASS, f"listening on {raw} ({label})")
        except Exception as exc:  # noqa: BLE001
            record("1. stack", FAIL, f"listening on {raw} ({label})", f"{type(exc).__name__}")
        finally:
            sock.close()


# ---------------------------------------------------------- rung 2: credentials
async def rung_credentials() -> None:
    print("\n[2] credentials and objects")
    api = LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        inbound = (await api.sip.list_inbound_trunk(psip.ListSIPInboundTrunkRequest())).items
        want_name = env("SIP_INBOUND_TRUNK", "photon-inbound")
        trunk = next((t for t in inbound if t.name == want_name), None)
        if trunk is None:
            record("2. credentials", FAIL, f"inbound trunk {want_name!r} exists",
                   f"found: {[t.name for t in inbound] or 'none'} - run 20-sip-objects.py")
        else:
            record("2. credentials", PASS, f"inbound trunk {want_name!r} exists",
                   trunk.sip_trunk_id)
            ok_user = trunk.auth_username == env("SIP_INBOUND_USER")
            ok_pass = (trunk.auth_password or "") == env("SIP_INBOUND_PASS")
            record("2. credentials", PASS if (ok_user and ok_pass) else FAIL,
                   "inbound credentials match .env",
                   f"user={ok_user} pass={ok_pass} "
                   f"(len trunk={len(trunk.auth_password or '')}, "
                   f"len env={len(env('SIP_INBOUND_PASS'))})")
            record("2. credentials", PASS if list(trunk.numbers) else WARN,
                   "inbound trunk owns a number", str(list(trunk.numbers)))

        outbound = (await api.sip.list_outbound_trunk(psip.ListSIPOutboundTrunkRequest())).items
        want_out = env("SIP_OUTBOUND_TRUNK", "photon-outbound")
        out = next((t for t in outbound if t.name == want_out), None)
        if out is None:
            record("2. credentials", FAIL, f"outbound trunk {want_out!r} exists",
                   f"found: {[t.name for t in outbound] or 'none'} - run 30-photon.py")
        else:
            record("2. credentials", PASS, f"outbound trunk {want_out!r} exists",
                   f"{out.address} {psip.SIPTransport.Name(out.transport)}")
            ok_user = out.auth_username == env("SIP_OUTBOUND_USER")
            ok_pass = (out.auth_password or "") == env("SIP_OUTBOUND_PASS")
            record("2. credentials", PASS if (ok_user and ok_pass) else FAIL,
                   "outbound credentials match .env",
                   f"user={ok_user} pass={ok_pass} "
                   f"(len trunk={len(out.auth_password or '')}, "
                   f"len env={len(env('SIP_OUTBOUND_PASS'))})")

        rules = (await api.sip.list_dispatch_rule(psip.ListSIPDispatchRuleRequest())).items
        want_rule = env("SIP_DISPATCH_RULE", "photon-inbound")
        rule = next((r for r in rules if r.name == want_rule), None)
        if rule is None:
            record("2. credentials", FAIL, f"dispatch rule {want_rule!r} exists",
                   f"found: {[r.name for r in rules] or 'none'} - run 20-sip-objects.py")
        else:
            prefix = getattr(getattr(rule.rule, "dispatch_rule_individual", None),
                             "room_prefix", "")
            record("2. credentials", PASS, f"dispatch rule {want_rule!r} exists",
                   f"room_prefix={prefix!r}")
    finally:
        await api.aclose()

    # The other half of the credential story: does the provider still point here?
    # Both sides can be individually correct and still not agree.
    if env("PHOTON_API_KEY") and env("PHOTON_PROJECT_ID") and env("PHOTON_VOICE_PROFILE_ID"):
        path = (f"/v1/projects/{env('PHOTON_PROJECT_ID')}/platforms/voice/profiles/"
                f"{env('PHOTON_VOICE_PROFILE_ID')}")

        def provider_get(api_path: str) -> tuple[int | None, bytes]:
            req = urllib.request.Request(
                "https://api.photon.codes" + api_path,
                headers={"Authorization": "Bearer " + env("PHOTON_API_KEY"),
                         "Accept": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
            except Exception as exc:  # noqa: BLE001
                return None, f"{type(exc).__name__}: {exc}".encode()

        status, body = provider_get(path)
        if status != 200:
            # Measured against this API, and each one points somewhere different:
            # 401 = the key is missing or not accepted, 403 = the request carried no
            # User-Agent (their edge blocks bare clients), 404 = the project or profile
            # id is wrong. They separate cleanly, so name the one thing to fix rather
            # than "the provider refused us".
            if status == 401:
                hint = " - PHOTON_API_KEY is missing, expired, or not accepted"
            elif status == 403:
                hint = (" - this API answers 403 to a client that sends no User-Agent; "
                        "this check sends one, so something stripped the header (a proxy?) "
                        "or another client is hitting the API")
            elif status == 404:
                hint = (" - no such route: PHOTON_PROJECT_ID or PHOTON_VOICE_PROFILE_ID is "
                        "wrong, or the profile was deleted")
            elif status == 422:
                hint = (" - the id is malformed (wrong prefix or length) - copy "
                        "PHOTON_VOICE_PROFILE_ID out of the provider dashboard again")
            else:
                hint = ""
            record("2. credentials", FAIL, "provider profile readable",
                   f"HTTP {status}{hint}")
            return
        try:
            profile = json.loads(body or b"{}")
        except ValueError as exc:
            record("2. credentials", FAIL, "provider profile readable",
                   f"HTTP 200 but the body is not JSON: {exc}")
            return
        # The profile nests everything under "configuration" - reading the top
        # level finds nothing and reports a perfectly good setup as broken.
        conf = profile.get("configuration") or {}
        inbound = conf.get("inbound") or {}
        want = f"sip:{env('SIP_DOMAIN')}:{env('SIP_TLS_PORT', '5061')};transport=tls"
        got = inbound.get("destinationUri") or ""
        record("2. credentials", PASS if got == want else FAIL,
               "provider points at this host",
               f"destinationUri={got!r}" if got else "no inbound destination set - run 30-photon.py")

        # What Photon presents when it sends an INVITE to our trunk. A provider that
        # has a destination but no credential invites unauthenticated, the trunk
        # rejects it with 403, and the caller hears "this number is not available" -
        # so a missing username is a broken inbound, not a note to read later. The
        # password is never returned by the API; the trunk half of that comparison
        # happened above, against .env.
        creds = inbound.get("credentials")
        got_user = creds.get("username") if isinstance(creds, dict) else ""
        trunk_user = (trunk.auth_username if trunk else "") or ""
        env_user = env("SIP_INBOUND_USER") or ""
        if got_user:
            agree = got_user == env_user == trunk_user
            record("2. credentials", PASS if agree else FAIL,
                   "provider credential matches our trunk",
                   f"provider={got_user!r} trunk={trunk_user!r} env={env_user!r} "
                   f"(the provider never returns the password, so that half is the "
                   f"trunk-vs-.env check above)")
        elif inbound:
            record("2. credentials", FAIL, "provider credential matches our trunk",
                   "the provider has an inbound destination but no credential username, "
                   "so it will INVITE unauthenticated and every inbound call will be "
                   "rejected - re-run bootstrap/30-photon.py")
        else:
            record("2. credentials", WARN, "provider credential matches our trunk",
                   "the provider has no inbound configuration yet - run "
                   "bootstrap/30-photon.py")

        # The outbound side is asymmetric: the provider's profile advertises a
        # rotated username (v...) while the trunk authenticates with the one it was
        # created with, and calls connect anyway. Report the difference, do not
        # fail on it - the only authority here is a real call.
        auth = ((conf.get("outbound") or {}).get("authentication") or {})
        adv = auth.get("username") or ""
        ours = env("SIP_OUTBOUND_USER")
        if adv and ours and adv != ours:
            record("2. credentials", WARN, "provider outbound username differs",
                   f"provider advertises a different username than the trunk sends "
                   f"(len {len(adv)} vs {len(ours)}); calls still connect, so this is "
                   f"informational - if outbound starts failing with 401, re-run "
                   f"bootstrap/30-photon.py to re-sync it")
        record("2. credentials", PASS if profile.get("lineCount") else FAIL,
               "provider line is voice-eligible", f"lineCount={profile.get('lineCount')}")


# --------------------------------------------------------------- rung 3: worker
def rung_worker() -> None:
    print("\n[3] worker")
    unit = os.environ.get("UNIT_NAME", "livekit-agent") + ".service"
    code, state = sh("systemctl", "--user", "is-active", unit)
    record("3. worker", PASS if state == "active" else FAIL, f"{unit} active",
           state or "unknown")

    # The worker's HTTP health port is the live proof it is up: it only listens
    # once the process has registered with the API and can be dispatched a job.
    port = env("AGENT_WORKER_PORT", "8083")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            record("3. worker", PASS, f"worker health port {port} answering",
                   f"HTTP {r.status}")
    except urllib.error.HTTPError as exc:
        record("3. worker", PASS, f"worker health port {port} answering",
               f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        record("3. worker", FAIL, f"worker health port {port} answering",
               f"{type(exc).__name__} - nothing listening; is the worker running?")

    # Registration is logged at startup only, so a unit that has been up for days
    # has no recent line. Search the whole unit log, not a recent window.
    code, logs = sh("journalctl", "--user", "-u", unit, "--no-pager")
    hits = re.findall(r"(\w{3} +\d+ \d\d:\d\d:\d\d).*\"message\": \"registered worker\"", logs)
    if hits:
        record("3. worker", PASS, "worker registered with the API",
               f"registered {len(hits)} time(s) since the unit was installed, "
               f"last at {hits[-1]}")
    else:
        record("3. worker", WARN, "worker registered with the API",
               "no registration line in this unit's log - if the health port above "
               "answered, the worker is up and this log was rotated")

    call_dir = Path(env("AGENT_CALL_LOG_DIR") or (REPO / "calls"))
    if call_dir.is_dir() and (call_dir.stat().st_mode & 0o200):
        count = len(list(call_dir.glob("*.jsonl")))
        record("3. worker", PASS, "transcript directory writable",
               f"{call_dir} ({count} call(s) recorded)")
    else:
        record("3. worker", FAIL, "transcript directory writable", f"{call_dir}")


async def main() -> int:
    print(f"photon-livekit-voice ladder  ({REPO})")
    rung_stack()
    await rung_credentials()
    rung_worker()

    print("\n=== ladder summary ===")
    failed = 0
    for rung in sorted(rungs):
        rows = rungs[rung]
        bad = [r for r in rows if r[0] == FAIL]
        failed += len(bad)
        state = "OK" if not bad else f"{len(bad)} FAIL"
        print(f"  {rung:16} {len(rows) - len(bad)}/{len(rows)} passed  {state}")
    if failed:
        print("\nRungs 1-3 are not clean, so a call would fail in a way that looks like "
              "something else. Fix these first:")
        for rung in sorted(rungs):
            for status, name, detail in rungs[rung]:
                if status == FAIL:
                    print(f"  * {name}: {detail}")
        return 1
    print("\nRungs 1-3 clean. Next rung needs a real call:")
    print("  venv/bin/python tests/room_test.py          # a room, no telephony")
    print("  venv/bin/python cli/call_cli.py --to <num> --instruction '...'")
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
