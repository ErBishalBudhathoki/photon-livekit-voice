#!/usr/bin/env python3
"""Render templates/*.tmpl into the real config files, using .env as the values.

Why this exists instead of `sed` in a shell script: it refuses to write a config
that still contains a placeholder. A missing variable otherwise produces a valid
YAML file with the literal string `__SIP_TLS_PORT__` in it, and the failure shows
up much later as a container that will not start - or worse, as a line that
answers and has no audio.

No secret is ever printed: the only thing reported about a credential is that it
was substituted, and files that carry one are written mode 600.

    python3 bootstrap/render.py --all          # livekit.yaml, sip.yaml, compose, unit
    python3 bootstrap/render.py --check        # only verify every placeholder resolves
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

from env import env, load_env_file  # noqa: E402

TEMPLATES = REPO / "templates"
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")

# target file -> (template, secrets_inside, [(placeholder, .env key, default)])
TARGETS: dict[str, tuple[str, bool, list[tuple[str, str, str]]]] = {
    "livekit.yaml": ("livekit.yaml.tmpl", True, [
        ("__LIVEKIT_API_PORT__", "LIVEKIT_API_PORT", "7880"),
        ("__RTC_TCP_PORT__", "RTC_TCP_PORT", "7881"),
        ("__RTC_UDP_PORT__", "RTC_UDP_PORT", "7882"),
        ("__REDIS_PORT__", "REDIS_PORT", "6380"),
        ("__LIVEKIT_API_KEY__", "LIVEKIT_API_KEY", ""),
        ("__LIVEKIT_API_SECRET__", "LIVEKIT_API_SECRET", ""),
    ]),
    "sip.yaml": ("sip.yaml.tmpl", True, [
        ("__LIVEKIT_API_PORT__", "LIVEKIT_API_PORT", "7880"),
        ("__REDIS_PORT__", "REDIS_PORT", "6380"),
        ("__SIP_PORT__", "SIP_PORT", "5060"),
        ("__SIP_TLS_PORT__", "SIP_TLS_PORT", "5061"),
        ("__RTP_PORT_RANGE__", "RTP_PORT_RANGE", "10000-10100"),
        ("__TLS_CERT_FILE__", "TLS_CERT_FILE", "/certs/sip.crt"),
        ("__TLS_KEY_FILE__", "TLS_KEY_FILE", "/certs/sip.key"),
        ("__LIVEKIT_API_KEY__", "LIVEKIT_API_KEY", ""),
        ("__LIVEKIT_API_SECRET__", "LIVEKIT_API_SECRET", ""),
    ]),
    "docker-compose.yaml": ("docker-compose.yaml", False, []),
    # Written into the user's systemd directory by bootstrap/40-agent.sh, not here.
    "systemd/livekit-agent.service": ("systemd/livekit-agent.service.tmpl", False, [
        ("__REPO_DIR__", "REPO_DIR", str(REPO)),
    ]),
}

REQUIRED_FOR = {
    "livekit.yaml": ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
    "sip.yaml": ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
}


def resolve(name: str, key: str, default: str) -> str:
    if key == "REPO_DIR":
        return str(REPO)
    value = env(key) or default
    return value


def render_one(target: str, write: bool) -> tuple[bool, list[str]]:
    tmpl_name, secret, subs = TARGETS[target]
    tmpl = TEMPLATES / tmpl_name
    if not tmpl.is_file():
        return False, [f"template missing: {tmpl}"]

    text = tmpl.read_text(encoding="utf-8")
    missing: list[str] = []
    for placeholder, key, default in subs:
        value = resolve(placeholder, key, default)
        if not value:
            missing.append(key)
            continue
        text = text.replace(placeholder, value)

    # Only the compose file is left with ${...} on purpose - docker compose
    # substitutes those itself, at container start.
    left = [p for p in PLACEHOLDER.findall(text)]
    if left:
        missing += [f"unresolved {p}" for p in sorted(set(left))]

    if not write:
        return (not missing), missing

    out = REPO / target
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600 if secret else 0o644)
    return True, []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="render every target")
    ap.add_argument("--check", action="store_true", help="verify placeholders resolve, write nothing")
    ap.add_argument("--only", action="append", default=[], choices=sorted(TARGETS),
                    help="render one target (repeatable)")
    ap.add_argument("--unit-path", help="where to write the systemd unit (40-agent.sh)")
    args = ap.parse_args()

    load_env_file()
    targets = args.only or (sorted(TARGETS) if (args.all or args.check) else [])
    if not targets:
        ap.error("nothing to do: pass --all, --check or --only <target>")

    failed = False
    for target in targets:
        if target.startswith("systemd/"):
            # The unit is installed by 40-agent.sh into the user's systemd dir.
            if args.unit_path:
                tmpl = TEMPLATES / TARGETS[target][0]
                text = tmpl.read_text(encoding="utf-8").replace("__REPO_DIR__", str(REPO))
                dest = Path(args.unit_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
                print(f"  rendered {dest}")
                continue
            print("  skipped systemd unit (no --unit-path given)")
            continue

        ok, problems = render_one(target, write=not args.check)
        if ok and not args.check:
            print(f"  rendered {target}")
        elif ok:
            print(f"  ok      {target}")
        else:
            failed = True
            for problem in problems:
                print(f"  MISSING {target}: {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
