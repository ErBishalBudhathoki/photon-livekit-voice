#!/usr/bin/env python3
"""Read the provider's own view of the account, with secrets redacted.

Useful when a call fails at the provider and you need to know what the provider
thinks it has: which resources exist, whether the voice line is active, what the
profile's inbound destination and version are. Anything whose key looks like a
credential is replaced with *** before printing, so the output is safe to paste
into a bug report.

    venv/bin/python tests/photon_line_probe.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

load_env_file()

BASE = "https://api.photon.codes"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

PROJECT = env("PHOTON_PROJECT_ID")
PROFILE = env("PHOTON_VOICE_PROFILE_ID")


def get(path: str):
    req = urllib.request.Request(BASE + path, method="GET")
    req.add_header("Authorization", "Bearer " + env("PHOTON_API_KEY"))
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def redact(obj):
    """Never print anything that could be a credential."""
    if isinstance(obj, dict):
        return {k: ("***" if any(s in k.lower() for s in
                                 ("password", "secret", "token", "key", "credential"))
                    else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


if not (env("PHOTON_API_KEY") and PROJECT):
    sys.exit("PHOTON_API_KEY / PHOTON_PROJECT_ID are not set in .env")

paths = [f"/v1/projects/{PROJECT}/platforms/resources"]
if PROFILE:
    paths.append(f"/v1/projects/{PROJECT}/platforms/voice/profiles/{PROFILE}")
else:
    print("PHOTON_VOICE_PROFILE_ID is not set - skipping the profile view\n")

for path in paths:
    status, body = get(path)
    print("=" * 70)
    print(path, "->", status)
    if isinstance(body, (dict, list)):
        print(json.dumps(redact(body), indent=2)[:2500])
    else:
        print(body)
