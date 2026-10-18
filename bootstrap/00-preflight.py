#!/usr/bin/env python3
"""Gate 0: everything that must be true BEFORE anything is started.

Every check here exists because it failed silently at least once and cost an hour
of chasing the wrong layer: a Cloudflare-proxied A record that eats INVITEs, a
certificate that covers a different name, a port already held by something else,
a provider key that is present but has no voice line behind it.

Stdlib only, so it runs before any venv exists. Nothing here mutates anything and
no secret value is printed - only presence, length or a masked form.

    python3 bootstrap/00-preflight.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def mask(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return ("*" * (len(d) - 4) + d[-4:]) if len(d) > 4 else "(none)"


# --------------------------------------------------------------------- .env
def check_env_file() -> bool:
    path = REPO / ".env"
    if not path.is_file():
        record(FAIL, ".env exists",
               f"missing - run: cp .env.example .env && chmod 600 .env, then fill it in")
        return False
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        record(WARN, ".env permissions", f"mode {mode:o} - it holds credentials, use 600")
    else:
        record(PASS, ".env exists", f"mode {mode:o}")
    return True


def check_required_keys() -> bool:
    # Split by what the failure actually is, because the fixes are unrelated.
    provider = {
        "PHOTON_API_KEY": "Photon API key",
        "PHOTON_PROJECT_ID": "Photon project id",
        "PHOTON_VOICE_PROFILE_ID": "Photon voice profile id",
    }
    voice = {"GEMINI_API_KEY": "Gemini Live API key"}
    host = {
        "SIP_DOMAIN": "public SIP hostname",
        "ACME_EMAIL": "ACME contact email",
        "OWNER_NUMBER": "your own number (used by /callme)",
    }
    ok = True
    for group, label in ((provider, "provider"), (voice, "voice"), (host, "host")):
        missing = [name for name in group if not env(name)]
        if missing:
            ok = False
            record(FAIL, f"{label} config complete",
                   "empty: " + ", ".join(missing) + f" (fix: {group[missing[0]]})")
        else:
            record(PASS, f"{label} config complete", f"{len(group)} keys present")
    if env("OWNER_NUMBER"):
        record(PASS, "owner number", mask(env("OWNER_NUMBER")))
    return ok


# ---------------------------------------------------------------------- host
def public_ip() -> str:
    if env("PUBLIC_IP"):
        return env("PUBLIC_IP")
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                candidate = r.read().decode().strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", candidate):
                return candidate
        except Exception:  # noqa: BLE001
            continue
    return ""


def cloudflare_ranges() -> list[str]:
    try:
        with urllib.request.urlopen("https://api.cloudflare.com/client/v4/ips", timeout=10) as r:
            data = json.loads(r.read())
        return list(data.get("result", {}).get("ipv4_cidrs", []))
    except Exception:  # noqa: BLE001
        return []


def in_cidr(ip: str, cidr: str) -> bool:
    try:
        net, bits = cidr.split("/")
        mask_int = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF
        to_int = lambda a: sum(int(p) << (8 * (3 - i)) for i, p in enumerate(a.split(".")))  # noqa: E731
        return (to_int(ip) & mask_int) == (to_int(net) & mask_int)
    except Exception:  # noqa: BLE001
        return False


def check_dns() -> None:
    """The single most expensive silent failure: SIP cannot cross a CF proxy."""
    domain = env("SIP_DOMAIN")
    if not domain:
        return
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except socket.gaierror as exc:
        record(FAIL, f"DNS {domain}", f"does not resolve ({exc}) - create a grey-cloud A record")
        return

    proxied = [ip for ip in addrs if any(in_cidr(ip, c) for c in cloudflare_ranges())]
    if proxied:
        record(FAIL, f"DNS {domain} is not proxied",
               f"resolves to Cloudflare ({', '.join(proxied)}). SIP cannot traverse the proxy: "
               f"set the record to DNS-only (grey cloud). Calls fail silently otherwise.")
        return
    record(PASS, f"DNS {domain} is not proxied", ", ".join(addrs))

    mine = public_ip()
    if not mine:
        record(WARN, "public IP", "could not determine it; set PUBLIC_IP in .env")
        return
    if mine in addrs:
        record(PASS, "DNS points at this host", f"{mine}")
    else:
        record(FAIL, "DNS points at this host",
               f"{domain} -> {', '.join(addrs)} but this host is {mine}. The carrier would "
               f"deliver calls to a different machine.")


def check_cert() -> None:
    cert = REPO / "certs" / "sip.crt"
    key = REPO / "certs" / "sip.key"
    domain = env("SIP_DOMAIN") or ""
    if not cert.is_file():
        record(FAIL, "TLS certificate present",
               "certs/sip.crt missing - the carrier's TLS trunk needs a publicly trusted "
               "cert. DNS-01 is the practical route: bootstrap/cert-acme.sh")
        return
    record(PASS, "TLS certificate present", f"{cert.stat().st_size} bytes")
    if not key.is_file():
        record(FAIL, "TLS private key present", "certs/sip.key missing")
    else:
        record(PASS, "TLS private key present", f"mode {key.stat().st_mode & 0o777:o}")
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-dates", "-ext",
             "subjectAltName"],
            capture_output=True, text=True, timeout=20, check=True).stdout
    except Exception as exc:  # noqa: BLE001
        record(WARN, "TLS certificate readable", f"openssl could not read it: {exc}")
        return
    names = re.findall(r"DNS:([^,\s]+)", out)
    if domain and domain in names:
        record(PASS, "certificate covers the SIP hostname", domain)
    elif domain:
        record(FAIL, "certificate covers the SIP hostname",
               f"{domain} not in {names or '(no SAN)'} - the carrier's TLS handshake will "
               f"fail and every call dies at the trunk")
    end = re.search(r"notAfter=(.+)", out)
    if end:
        try:
            expiry = datetime.strptime(end.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc)
            days = (expiry - datetime.now(timezone.utc)).days
            record(PASS if days > 14 else FAIL, "certificate validity",
                   f"{days} days left (expires {expiry.date()})")
        except ValueError:
            pass


def port_owner(port: int) -> str:
    """Which process holds a TCP port, if any. Empty means free."""
    for cmd in (["ss", "-lntpH", f"sport = :{port}"], ["ss", "-lntp", f"sport = :{port}"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001
            continue
        for line in out.splitlines():
            if f":{port} " in line or f":{port}\t" in line:
                match = re.search(r'users:\(\("([^"]+)"', line)
                return match.group(1) if match else "something"
    return ""


def docker_containers() -> set:
    """Names of running containers, or empty when docker cannot be asked."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def check_ports() -> None:
    """Ports we need, unless our own stack already holds them."""
    ours = {"livekit-server", "livekit-sip", "livekit-redis", "docker-proxy"}
    running = docker_containers() & ours
    wanted = [
        ("LIVEKIT_API_PORT", "LiveKit API/WS"),
        ("RTC_TCP_PORT", "LiveKit RTC TCP"),
        ("REDIS_PORT", "LiveKit state (redis)"),
        ("SIP_PORT", "SIP"),
        ("SIP_TLS_PORT", "SIP TLS"),
    ]
    for key, label in wanted:
        raw = env(key)
        if not raw:
            continue
        port = int(raw)
        holder = port_owner(port)
        if not holder:
            record(PASS, f"port {port} free ({label})", "unused")
        elif holder in ours:
            record(PASS, f"port {port} free ({label})", f"already served by {holder}")
        elif holder == "something" and running:
            # Host-networked containers listen in their own network namespace, so
            # `ss` cannot name the process that holds the port. A running
            # livekit-* container is the likely holder, and re-running the
            # installer over a working stack is normal - do not fail on it.
            record(WARN, f"port {port} free ({label})",
                   f"held, and {'/'.join(sorted(running))} is running - if that is this "
                   f"stack, re-running the installer is fine; otherwise move it or "
                   f"point {key} elsewhere")
        else:
            record(FAIL, f"port {port} free ({label})",
                   f"held by {holder} - move it, or point {key} elsewhere")


def check_tooling() -> None:
    for binary, why in (("docker", "the stack runs in containers"),
                        ("openssl", "certificate checks and key generation")):
        if shutil.which(binary):
            record(PASS, f"{binary} available", why)
        else:
            record(FAIL, f"{binary} available", f"not on PATH - needed because {why}")
    compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if compose.returncode == 0:
        record(PASS, "docker compose plugin", compose.stdout.strip().splitlines()[0])
    else:
        record(FAIL, "docker compose plugin", "`docker compose` failed - install the plugin")

    for candidate in ("python3.10", "python3.11", "python3.12"):
        if shutil.which(candidate):
            record(PASS, "python for the worker", candidate)
            break
    else:
        record(FAIL, "python for the worker",
               "no python3.10+ found - the worker needs 3.10 or newer")
    if shutil.which("uv"):
        record(PASS, "uv available", "used to build the worker venv")


def check_provider_line() -> None:
    """A key with no voice-eligible line is the failure that looks like a bug."""
    key, project = env("PHOTON_API_KEY"), env("PHOTON_PROJECT_ID")
    if not (key and project):
        return
    req = urllib.request.Request(
        f"https://api.photon.codes/v1/projects/{project}/platforms/resources",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                 # Their edge answers bare clients with a Cloudflare 1010.
                 "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        record(FAIL, "provider key accepted", f"HTTP {exc.code} - check PHOTON_API_KEY")
        return
    except Exception as exc:  # noqa: BLE001
        record(WARN, "provider key accepted", f"{type(exc).__name__}: {exc}")
        return
    record(PASS, "provider key accepted", "resources endpoint answered")
    lines = [res for res in (data.get("resources") or [])
             if res.get("type") == "voip_line" and res.get("state") == "active"]
    if not lines:
        record(FAIL, "provider has a voice-eligible line",
               "no active voip_line on this project - calls cannot be placed or received. "
               "Get a number in the provider dashboard first; no script here can create one.")
        return
    number = (lines[0].get("detail") or {}).get("phoneNumber") or ""
    record(PASS, "provider has a voice-eligible line", f"{mask(number)} ({len(lines)} line)")


def main() -> int:
    print(f"photon-livekit-voice preflight  ({REPO})")
    load_env_file()
    print("\n[.env]")
    if not check_env_file():
        print("\nCreate the .env first, then re-run. Nothing else can be checked without it.")
        return 1
    print("\n[configuration]")
    check_required_keys()
    print("\n[host]")
    check_dns()
    check_cert()
    check_ports()
    check_tooling()
    print("\n[provider]")
    check_provider_line()

    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    print(f"\n=== {len(results) - len(failed) - len(warned)} passed, "
          f"{len(failed)} failed, {len(warned)} warned ===")
    if failed:
        print("Fix the FAILs above before starting the stack - each one is a silent failure:")
        for _, name, detail in failed:
            print(f"  * {name}: {detail}")
        return 1
    print("Preflight clean. Next: bash bootstrap/10-stack.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
